"""Configuration page and the Prowlarr proxy endpoints.

The three proxy/verify endpoints answer JSON by default and HTML when asked with
``?format=html``. JSON keeps them usable as a real API; the HTML variant is what
the page itself consumes, so a dropdown can refresh straight into the DOM after
the Prowlarr connection changes, with no glue JavaScript.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ....db.models import Config
from ....db.session import get_config
from ....prowlarr.client import ProwlarrClient, ProwlarrError
from ....settings import SEERR_URL_ENV, seerr_url
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

Format = Annotated[Literal["json", "html"], Query()]

#: Where Seerr posts. Named here rather than written out in the template, so
#: the path an admin is told to paste into Seerr and the path the router
#: actually serves cannot drift apart without this line being wrong.
SEERR_WEBHOOK_PATH = "/webhooks/seerr"

#: How many bytes of entropy a generated webhook secret carries. Comfortably
#: more than the 128 bits that would do, because nobody has to type it: it is
#: copied out of this page and pasted into Seerr once.
SEERR_WEBHOOK_SECRET_BYTES = 32


async def _prowlarr(state: StateDep, db: DbDep) -> ProwlarrClient | None:
    """A client for the configured Prowlarr, or ``None`` if it is not set up."""
    config = await get_config(db)
    if not config.prowlarr_url or not config.prowlarr_api_key:
        return None
    return ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key, client=state.http)


def _webhook_context(request: Request, config: Config) -> dict[str, object]:
    """What the Seerr-webhook section renders from.

    The URL is assembled from the request the admin is making *right now*,
    which is the only address this process has any evidence about. It is a
    suggestion and the page says so: an admin reaching the console over a LAN
    address while Seerr sees it through a reverse proxy has to substitute the
    one Seerr can actually resolve, and no amount of guessing here would know
    that.
    """
    return {
        "config": config,
        "webhook_url": str(request.base_url).rstrip("/") + SEERR_WEBHOOK_PATH,
        "webhook_path": SEERR_WEBHOOK_PATH,
    }


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            **_webhook_context(request, config),
            "admin": admin,
            # Straight from the environment, never from the database — the page
            # shows the one live answer rather than a copy that could drift.
            "seerr_url": seerr_url(),
            "seerr_url_env": SEERR_URL_ENV,
            "title": "Configuration",
            "nav": "config",
        },
    )


@router.post("/config", response_class=HTMLResponse)
async def save_config(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    prowlarr_url: str = Form(default=""),
    prowlarr_api_key: str = Form(default=""),
    preferred_indexer_id: str | None = Form(default=None),
    tmdb_bearer_token: str = Form(default=""),
) -> Response:
    config = await get_config(db)
    config.prowlarr_url = prowlarr_url.strip().rstrip("/") or None

    # An empty key field means "leave it alone", so the saved key is never
    # rendered back into the page and cannot be blanked by a careless save.
    if prowlarr_api_key.strip():
        config.prowlarr_api_key = prowlarr_api_key.strip()

    if tmdb_bearer_token.strip():
        config.tmdb_bearer_token = tmdb_bearer_token.strip()

    # Three states, not two. Empty means the "All indexers" default, which is
    # null and not a sentinel — but *absent* means the page could not offer a
    # choice at all, because the select is disabled until Prowlarr's indexer
    # list loads and a disabled field is never submitted. Treating that as
    # "All indexers" would clear a saved preference every time an admin edited
    # something else on this page while Prowlarr was down.
    if preferred_indexer_id is not None:
        config.preferred_indexer_id = (
            int(preferred_indexer_id) if preferred_indexer_id.strip().isdigit() else None
        )

    return templates.TemplateResponse(
        request,
        "partials/saved.html",
        {"message": "Configuration saved."},
    )


@router.post("/config/seerr-webhook", response_class=HTMLResponse)
async def generate_seerr_webhook_secret(
    request: Request, db: DbDep, admin: AdminPageDep
) -> Response:
    """Issue a new secret for ``POST /webhooks/seerr``, replacing any it had.

    One button for both "switch this on" and "rotate it", because they are the
    same operation and an admin who wants a fresh secret wants exactly what an
    admin switching it on wants. Rotating breaks the existing Seerr
    configuration until the new value is pasted over the old one, which is the
    point of rotating and is said on the page.

    Generated rather than typed: there is no second party to agree a value with
    — Seerr accepts whatever string it is given — so a human-chosen one would
    only ever be weaker.
    """
    config = await get_config(db)
    config.seerr_webhook_secret = secrets.token_urlsafe(SEERR_WEBHOOK_SECRET_BYTES)
    await db.flush()

    logger.info("issued a new Seerr webhook secret")
    return templates.TemplateResponse(
        request, "partials/seerr_webhook.html", _webhook_context(request, config)
    )


@router.post("/config/seerr-webhook/disable", response_class=HTMLResponse)
async def disable_seerr_webhook(
    request: Request, db: DbDep, admin: AdminPageDep
) -> Response:
    """Forget the secret, which is what switches the endpoint off.

    Nothing else has to be torn down: with no secret stored there is no value
    any caller could present, so the endpoint refuses everyone. Seerr goes on
    posting until someone turns its webhook off there as well, and gets a 503
    for its trouble — harmless, and visible in Seerr's own logs, which is the
    right place for an admin to notice they only did half of it.
    """
    config = await get_config(db)
    config.seerr_webhook_secret = None
    await db.flush()

    logger.info("switched the Seerr webhook off")
    return templates.TemplateResponse(
        request, "partials/seerr_webhook.html", _webhook_context(request, config)
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
