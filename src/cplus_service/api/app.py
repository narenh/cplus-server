"""FastAPI application factory.

Long-lived resources (the database engine and the two outbound HTTP clients —
one for Prowlarr, one kept separate for Seerr) are created once per process in
the lifespan and hung off ``app.state``. Nothing per-request owns them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine

from ..auth.sessions import purge_expired_sessions
from ..bootstrap import ensure_request_action
from ..db.session import create_all, create_engine, create_session_factory, session_scope
from ..web import STATIC_DIR
from .routes import admin, grab, manager, register, request, seerr, titles
from .state import AppState

logger = logging.getLogger(__name__)


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

        if create_schema:
            await create_all(active_engine)

        app.state.cplus = AppState(
            engine=active_engine,
            sessionmaker=sessionmaker,
            http=http,
            seerr_http=seerr_http,
        )

        async with session_scope(sessionmaker) as session:
            await ensure_request_action(session)
            await purge_expired_sessions(session)

        try:
            yield
        finally:
            await http.aclose()
            await seerr_http.aclose()
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

    app.include_router(register.router)
    app.include_router(titles.router)
    app.include_router(grab.router)
    app.include_router(manager.router)
    app.include_router(request.router)
    app.include_router(seerr.router)
    app.include_router(admin.router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """There is no public web page; the only UI is the admin one."""
        return RedirectResponse("/admin/config")

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
