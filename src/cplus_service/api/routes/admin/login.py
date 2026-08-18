"""Admin sign-in: the Plex OAuth PIN flow, proxied.

Flow, all server-side except the popup:

1. ``GET /admin/login`` renders the page.
2. ``POST /admin/plex/pin`` asks plex.tv for a PIN and returns the URL the
   browser must open.
3. The page opens that URL in a popup and polls ``GET /admin/plex/pin/{id}``.
4. Once Plex hands back a token, the server validates it against Seerr, checks
   the ADMIN bit, and sets the session cookie — the Plex token itself never
   reaches page JavaScript.

Sign-in requires a Seerr URL, because Seerr — not Plex — decides who the admin
is. On a fresh install the login form asks for it and stores it only after it
has proven it can authenticate.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ....auth.identity import upsert_user
from ....auth.sessions import (
    SESSION_COOKIE_NAME,
    create_session,
    destroy_session,
    set_session_cookie,
)
from ....db.session import get_config
from ....plex.client import PlexError, PlexPinClient
from ....seerr.client import SeerrAuthError, SeerrClient, SeerrError
from ....web import templates
from ...deps import DbDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DbDep) -> Response:
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"seerr_url": config.seerr_url or "", "title": "Sign in"},
    )


@router.post("/logout")
async def logout(request: Request, db: DbDep) -> Response:
    await destroy_session(db, request.cookies.get(SESSION_COOKIE_NAME))
    response = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


async def _pin_client(state: StateDep, db: DbDep) -> PlexPinClient:
    """A PIN client bound to this install's stable Plex client identifier."""
    config = await get_config(db)
    if not config.plex_client_identifier:
        config.plex_client_identifier = str(uuid.uuid4())
    return PlexPinClient(config.plex_client_identifier, client=state.http)


@router.post("/plex/pin")
async def start_pin(
    state: StateDep,
    db: DbDep,
    seerr_url: str = Form(default=""),
) -> JSONResponse:
    """Begin the PIN flow, remembering the Seerr URL to validate against."""
    config = await get_config(db)
    target = (seerr_url or config.seerr_url or "").strip().rstrip("/")
    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Enter the URL of your Seerr instance first.",
        )

    plex = await _pin_client(state, db)
    try:
        pin_id, code = await plex.create_pin()
    except PlexError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach plex.tv: {exc}"
        ) from exc

    # Held in memory only for the life of the flow; nothing to clean up if the
    # admin abandons it, and a restart just means starting sign-in again.
    state.pending_plex_logins[pin_id] = target

    return JSONResponse(
        {"pin_id": pin_id, "code": code, "auth_url": plex.auth_url(code)}
    )


@router.get("/plex/pin/{pin_id}")
async def poll_pin(
    request: Request, pin_id: int, state: StateDep, db: DbDep
) -> JSONResponse:
    """Poll a PIN; on success, sign the admin in and set the session cookie."""
    seerr_url = state.pending_plex_logins.get(pin_id)
    if seerr_url is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That sign-in attempt has expired. Start again."
        )

    plex = await _pin_client(state, db)
    try:
        plex_token = await plex.check_pin(pin_id)
    except PlexError as exc:
        state.pending_plex_logins.pop(pin_id, None)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"plex.tv rejected the PIN: {exc}"
        ) from exc

    if not plex_token:
        return JSONResponse({"claimed": False})

    state.pending_plex_logins.pop(pin_id, None)

    seerr = SeerrClient(seerr_url, client=state.seerr_http)
    try:
        auth = await seerr.authenticate_plex(plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            exc.detail or "Seerr does not recognise this Plex account.",
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr at {seerr_url}: {exc}"
        ) from exc

    if not auth.user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That account is not the Seerr admin. The cplus-service web UI is admin-only.",
        )

    user = await upsert_user(db, auth)

    config = await get_config(db)
    if config.seerr_url != seerr_url:
        config.seerr_url = seerr_url

    token = await create_session(db, user.id)
    response = JSONResponse({"claimed": True, "redirect": "/admin/config"})
    set_session_cookie(response, request, token)
    return response
