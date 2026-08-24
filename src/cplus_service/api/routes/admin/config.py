"""Configuration page and the Prowlarr proxy endpoints.

The three proxy/verify endpoints answer JSON by default and HTML when asked with
``?format=html``. JSON keeps them usable as a real API; the HTML variant is what
the page itself consumes, so a dropdown can refresh straight into the DOM after
the Prowlarr connection changes, with no glue JavaScript.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ....auth.identity import apply_seerr_url_change
from ....auth.sessions import SESSION_COOKIE_NAME
from ....db.session import get_config
from ....prowlarr.client import ProwlarrClient, ProwlarrError
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

Format = Annotated[Literal["json", "html"], Query()]


async def _prowlarr(state: StateDep, db: DbDep) -> ProwlarrClient | None:
    """A client for the configured Prowlarr, or ``None`` if it is not set up."""
    config = await get_config(db)
    if not config.prowlarr_url or not config.prowlarr_api_key:
        return None
    return ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key, client=state.http)


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "config.html",
        {"config": config, "admin": admin, "title": "Configuration", "nav": "config"},
    )


@router.post("/config", response_class=HTMLResponse)
async def save_config(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    seerr_url: str = Form(default=""),
    prowlarr_url: str = Form(default=""),
    prowlarr_api_key: str = Form(default=""),
    preferred_indexer_id: str = Form(default=""),
) -> Response:
    config = await get_config(db)
    reconnected = await apply_seerr_url_change(
        db,
        config,
        seerr_url.strip().rstrip("/") or None,
        keep_session_token=request.cookies.get(SESSION_COOKIE_NAME),
    )
    config.prowlarr_url = prowlarr_url.strip().rstrip("/") or None

    # An empty key field means "leave it alone", so the saved key is never
    # rendered back into the page and cannot be blanked by a careless save.
    if prowlarr_api_key.strip():
        config.prowlarr_api_key = prowlarr_api_key.strip()

    # Empty means the "All indexers" default, which is null and not a sentinel.
    config.preferred_indexer_id = (
        int(preferred_indexer_id) if preferred_indexer_id.strip().isdigit() else None
    )

    message = "Configuration saved."
    if reconnected:
        message += " Every device and browser session has been signed out and will reconnect."

    return templates.TemplateResponse(
        request,
        "partials/saved.html",
        {"message": message},
    )


@router.post("/config/verify-prowlarr")
async def verify_prowlarr(
    request: Request, state: StateDep, db: DbDep, admin: AdminPageDep, format: Format = "json"
) -> Response:
    """Ping Prowlarr's system status with the saved credentials."""
    prowlarr = await _prowlarr(state, db)
    if prowlarr is None:
        result = {"ok": False, "message": "Set the Prowlarr URL and API key first, then save."}
    else:
        try:
            status_info = await prowlarr.verify_connection()
            name = status_info.app_name or "Prowlarr"
            version = status_info.version or "unknown version"
            result = {"ok": True, "message": f"Connected to {name} {version}."}
        except ProwlarrError as exc:
            result = {"ok": False, "message": str(exc)}

    if format == "html":
        return templates.TemplateResponse(request, "partials/verify.html", result)
    return JSONResponse(result)


@router.get("/prowlarr/indexers")
async def list_indexers(
    request: Request, state: StateDep, db: DbDep, admin: AdminPageDep, format: Format = "json"
) -> Response:
    """Prowlarr's indexers, for the preferred-indexer dropdown."""
    config = await get_config(db)
    prowlarr = await _prowlarr(state, db)

    indexers: list[dict[str, object]] = []
    error: str | None = None
    if prowlarr is None:
        error = "Prowlarr is not configured yet."
    else:
        try:
            indexers = [
                {"id": i.id, "name": i.name, "enable": i.enable, "protocol": i.protocol}
                for i in await prowlarr.list_indexers()
            ]
        except ProwlarrError as exc:
            error = str(exc)

    if format == "html":
        return templates.TemplateResponse(
            request,
            "partials/indexer_options.html",
            {
                "indexers": indexers,
                "error": error,
                "selected": config.preferred_indexer_id,
            },
        )
    return JSONResponse({"indexers": indexers, "error": error})


@router.get("/prowlarr/download-clients")
async def list_download_clients(
    request: Request, state: StateDep, db: DbDep, admin: AdminPageDep, format: Format = "json"
) -> Response:
    """Prowlarr's download clients, for the action form's dropdown."""
    prowlarr = await _prowlarr(state, db)

    clients: list[dict[str, object]] = []
    error: str | None = None
    if prowlarr is None:
        error = "Prowlarr is not configured yet."
    else:
        try:
            clients = [
                {"id": c.id, "name": c.name, "enable": c.enable, "protocol": c.protocol}
                for c in await prowlarr.list_download_clients()
            ]
        except ProwlarrError as exc:
            error = str(exc)

    if format == "html":
        return templates.TemplateResponse(
            request,
            "partials/download_client_options.html",
            {"clients": clients, "error": error, "selected": None},
        )
    return JSONResponse({"download_clients": clients, "error": error})
