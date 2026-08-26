"""Admin sign-in: the Plex OAuth PIN flow, proxied.

Flow, all server-side except the popup:

1. ``GET /admin/login`` renders the page.
2. ``POST /admin/plex/pin`` asks plex.tv for a PIN and returns the URL the
   browser must open.
3. The page opens that URL in a popup and polls ``GET /admin/plex/pin/{id}``.
4. Once Plex hands back a token, the server validates it against Seerr, checks
   the ADMIN bit, and sets the session cookie — the Plex token itself never
   reaches page JavaScript.

Seerr — not Plex — decides who the admin is, so **which Seerr is asked is not
the caller's to choose**. It comes from ``CPLUS_SEERR_URL`` in the environment
and nothing on this page can influence it; the login page only displays it, so
you can see what you are about to sign in against.

This used to be a field on the form, filled in on first run and remembered. It
could not stay one: these two endpoints take no authentication, because they
*are* how you authenticate. A visitor who could name the Seerr instance could
point a fresh install's login at a server of their own, have it answer "yes,
admin", and be handed a session here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
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
from ....settings import SEERR_URL_ENV, seerr_url
from ....web import templates
from ...deps import DbDep, StateDep
from ...state import AppState, PendingPlexLogin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

#: How long an unclaimed PIN sign-in is kept before being swept. Bounds the
#: growth of :attr:`~cplus_service.api.state.AppState.pending_plex_logins` —
#: ``POST /admin/plex/pin`` takes no auth, so without a sweep an abandoned or
#: repeatedly-triggered sign-in would sit in memory until restart. Deliberately
#: tighter than plex.tv's own PIN lifetime (a ``strong`` PIN, the kind this app
#: requests, gets ``expiresIn: 1800`` from plex.tv) — there is no upside to
#: cplus outliving Plex's own expiry, and a shorter window bounds exposure if
#: an unclaimed ``pin_id`` were ever guessed.
PENDING_LOGIN_TTL = timedelta(minutes=15)


def _sweep_expired_logins(state: AppState) -> None:
    """Drop pending sign-ins older than :data:`PENDING_LOGIN_TTL`.

    Called on every new sign-in attempt rather than on a timer — there is no
    background task here, and this endpoint is exactly where the dict grows.
    """
    now = datetime.now(UTC)
    expired = [
        pin_id
        for pin_id, pending in state.pending_plex_logins.items()
        if now - pending.created_at > PENDING_LOGIN_TTL
    ]
    for pin_id in expired:
        del state.pending_plex_logins[pin_id]


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """The sign-in page. Read-only: it shows the Seerr host, it cannot set it."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {"seerr_url": seerr_url(), "title": "Sign in"},
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
async def start_pin(state: StateDep, db: DbDep) -> JSONResponse:
    """Begin the PIN flow.

    Takes no parameters at all — deliberately. The instance this sign-in will be
    validated against is whatever the environment says, both here and when the
    PIN is claimed, so there is nothing for a caller to supply.
    """
    if seerr_url() is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Seerr is not configured. Set {SEERR_URL_ENV} and restart.",
        )

    _sweep_expired_logins(state)

    plex = await _pin_client(state, db)
    try:
        pin_id, code = await plex.create_pin()
    except PlexError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach plex.tv: {exc}"
        ) from exc

    # Timestamped so an abandoned attempt is swept after PENDING_LOGIN_TTL
    # rather than held until restart. The timestamp is all it carries now: the
    # Seerr URL used to live here, back when the caller chose it.
    state.pending_plex_logins[pin_id] = PendingPlexLogin(created_at=datetime.now(UTC))

    return JSONResponse(
        {"pin_id": pin_id, "code": code, "auth_url": plex.auth_url(code)}
    )


@router.get("/plex/pin/{pin_id}")
async def poll_pin(
    request: Request, pin_id: int, state: StateDep, db: DbDep
) -> JSONResponse:
    """Poll a PIN; on success, sign the admin in and set the session cookie."""
    _sweep_expired_logins(state)
    if pin_id not in state.pending_plex_logins:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That sign-in attempt has expired. Start again."
        )

    target = seerr_url()
    if target is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Seerr is not configured. Set {SEERR_URL_ENV} and restart.",
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

    seerr = SeerrClient(target, client=state.seerr_http)
    try:
        auth = await seerr.authenticate_plex(plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            exc.detail or "Seerr does not recognise this Plex account.",
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr at {target}: {exc}"
        ) from exc

    if not auth.user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That account is not the Seerr admin. The cplus-service web UI is admin-only.",
        )

    user = await upsert_user(db, auth)
    token = await create_session(db, user.id)
    response = JSONResponse({"claimed": True, "redirect": "/admin/config"})
    set_session_cookie(response, request, token)
    return response
