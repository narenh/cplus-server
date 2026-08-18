"""``POST /auth`` — the webui sign-in, and nothing to do with tvOS.

The browser has no Plex token of its own, so it runs the standard Plex OAuth PIN
flow against plex.tv (generate a PIN, show the Plex popup, poll until claimed)
and posts the resulting token here. This service takes no part in that exchange
— it only ever sees the finished token.

Access is gated on Seerr's ADMIN permission **bit**, not on
``seerr_user_id == 1``: Seerr grants admin rights through the bitmask and the
owner account is not guaranteed to be user 1. The webui has no non-admin use
case, so a non-admin is rejected outright rather than given a reduced view.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from fastapi.responses import JSONResponse

from ...auth.identity import upsert_user
from ...auth.sessions import SESSION_COOKIE_NAME, create_session, destroy_session
from ...db.session import get_config
from ...seerr.client import SeerrAuthError, SeerrClient, SeerrError
from ..deps import DbDep, StateDep
from ..schemas import AuthRequest, AuthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webui"])

SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


@router.post("/auth", response_model=AuthResponse)
async def sign_in(
    state: StateDep,
    db: DbDep,
    body: AuthRequest,
    response: Response,
) -> AuthResponse:
    """Validate a Plex token, require the Seerr admin bit, and open a session."""
    config = await get_config(db)
    seerr_url = (body.seerr_url or config.seerr_url or "").rstrip("/")
    if not seerr_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Seerr URL configured. Supply seerr_url with the first sign-in.",
        )

    seerr = SeerrClient(seerr_url, client=state.seerr_http)
    try:
        auth = await seerr.authenticate_plex(body.plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr at {seerr_url}: {exc}"
        ) from exc

    if not auth.user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is not the Seerr admin. The cplus-service web UI is admin-only.",
        )

    user = await upsert_user(db, auth)

    # Persisted only now that it has been proven to work, so a typo cannot brick
    # the config. This is what makes first-run bootstrap possible: setting the
    # URL needs an admin session, and getting a session needs the URL.
    if body.seerr_url and config.seerr_url != seerr_url:
        config.seerr_url = seerr_url

    token = await create_session(db, user.id)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
    )

    return AuthResponse(
        seerr_user_id=user.seerr_user_id,
        username=user.plex_username,
        is_admin=True,
    )


@router.post("/auth/logout")
async def sign_out(
    db: DbDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> JSONResponse:
    """Drop the current browser session. Unknown sessions are not an error."""
    await destroy_session(db, session_cookie)
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
