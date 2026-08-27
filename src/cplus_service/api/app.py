"""FastAPI application factory.

Long-lived resources (the database engine and the three outbound HTTP clients —
one for Prowlarr, one kept separate for Seerr, one for the notification relay)
are created once per process in the lifespan and hung off ``app.state``.
Nothing per-request owns them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine

from ..auth.identity import sync_seerr_instance
from ..auth.sessions import purge_expired_sessions
from ..bootstrap import ensure_default_quality_profile, ensure_request_action
from ..db.session import (
    create_all,
    create_engine,
    create_session_factory,
    get_config,
    session_scope,
)
from ..settings import SEERR_URL_ENV, seerr_url
from ..web import STATIC_DIR
from .routes import (
    admin,
    capabilities,
    grab,
    manager,
    push_devices,
    register,
    request,
    seerr,
    titles,
)
from .state import AppState

logger = logging.getLogger(__name__)


class NoCacheStaticFiles(StaticFiles):
    """Static file serving with caching turned off.

    This install ships as a container image, not a CDN-backed site: a build
    replaces the static files in place, and a browser that cached the old
    ``app.css`` or ``login.html`` assets has no way to know they changed. The
    login page in particular gets looked at once per browser and then rarely
    again, which is exactly when a stale cache is most likely to stick.
    ``no-cache`` (not ``no-store``) still lets the browser revalidate with
    ETag/Last-Modified and get a cheap 304 when nothing changed — it just
    stops it from ever using a copy without asking first.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    *,
    db_path: Path | str | None = None,
    engine: AsyncEngine | None = None,
    create_schema: bool = False,
) -> FastAPI:
    """Build the application.

    ``engine`` lets tests inject their own; production passes ``db_path`` (or
    nothing, and picks up ``CPLUS_DB_PATH``). ``create_schema`` builds tables
    directly from the models — convenient for tests and first runs, but real
    deployments should run ``alembic upgrade head`` instead so the migration
    history stays authoritative.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_engine = engine is None
        active_engine = engine or create_engine(db_path)
        sessionmaker = create_session_factory(active_engine)
        http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        seerr_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        # The notification relay. The timeout is short because a push runs
        # after the response has already gone out: nothing is waiting on it,
        # but a hung connection would still pin a background task. Ordinary
        # HTTP/1.1 — the relay is the one that has to speak HTTP/2, to Apple.
        relay_http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

        if create_schema:
            await create_all(active_engine)

        app.state.cplus = AppState(
            engine=active_engine,
            sessionmaker=sessionmaker,
            http=http,
            seerr_http=seerr_http,
            relay_http=relay_http,
        )

        async with session_scope(sessionmaker) as session:
            await ensure_request_action(session)
            await ensure_default_quality_profile(session)
            # Before anything is served: if the deployment was repointed at a
            # different Seerr, every cached identity was resolved against an
            # instance that no longer decides anything here.
            if await sync_seerr_instance(session, await get_config(session)):
                logger.warning(
                    "%s changed since the last start — every device and browser"
                    " session has been signed out and must reconnect",
                    SEERR_URL_ENV,
                )
            await purge_expired_sessions(session)

        if seerr_url() is None:
            logger.error(
                "%s is not set: no one can sign in and no client can register."
                " Set it in the environment and restart.",
                SEERR_URL_ENV,
            )

        try:
            yield
        finally:
            await http.aclose()
            await seerr_http.aclose()
            await relay_http.aclose()
            if owns_engine:
                await active_engine.dispose()

    app = FastAPI(
        title="cplus-service",
        description=(
            "A permissioned Prowlarr front door for Seerr users. "
            "Talks to Prowlarr and Seerr only — never Sonarr or Radarr."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.include_router(capabilities.router)
    app.include_router(register.router)
    app.include_router(titles.router)
    app.include_router(grab.router)
    app.include_router(manager.router)
    app.include_router(push_devices.router)
    app.include_router(request.router)
    app.include_router(seerr.router)
    app.include_router(admin.router)

    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """There is no public web page; the only UI is the admin one."""
        return RedirectResponse("/admin/config")

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
