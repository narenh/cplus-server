"""``POST /request`` — the built-in Request action.

The odd one out in several ways, all deliberate:

* It supports TV as well as movies. Everything else here is movies-only.
* It never touches Prowlarr, so the preferred-indexer setting and quality
  profiles do not apply, and no ``grabs`` row is written.
* It is keyed by TMDB id, because that is what Seerr's request endpoint takes.
* It always validates the Plex token live rather than reading the cache,
  because it needs a Seerr session in order to file the request *as the user*.

It is a separate route from ``/grab`` precisely so none of that has to be
expressed as conditional branching on a shared one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ...auth.identity import authenticate_plex_token
from ...bootstrap import REQUEST_ACTION_NAME, get_request_action
from ...db.models import ActivityLog, EventType, Permission
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep
from ..schemas import RequestCreate, RequestResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


@router.post("/request", response_model=RequestResponse)
async def create_request(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    body: RequestCreate,
) -> RequestResponse | JSONResponse:
    """File a request in Seerr on the caller's behalf.

    No ``action_id`` is needed — there is exactly one Request action, so the
    server resolves it and checks the caller's permission on it directly.
    """
    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    action = await get_request_action(db)
    if action is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"The built-in {REQUEST_ACTION_NAME} action is not available on this server.",
        )

    granted = await db.execute(
        select(Permission).where(
            Permission.user_id == user.id, Permission.action_id == action.id
        )
    )
    if granted.scalars().first() is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"You do not have permission to use '{action.name}'",
        )

    try:
        result = await seerr.create_request(
            session_cookie=auth.session_cookie,
            media_type=body.type,
            tmdb_id=body.tmdb_id,
            seasons=body.seasons,
        )
    except SeerrError as exc:
        db.add(
            ActivityLog(
                user_id=user.id,
                event_type=EventType.GRAB,
                detail={
                    "kind": "request",
                    "action_id": action.id,
                    "tmdb_id": body.tmdb_id,
                    "type": body.type,
                    "seasons": body.seasons,
                    "success": False,
                    "error": exc.detail or str(exc),
                },
            )
        )
        # Pass a rejection (quota exceeded, already requested) through as a 4xx
        # with Seerr's own wording; anything else is an upstream fault.
        upstream = exc.status_code or 0
        code = upstream if 400 <= upstream < 500 else status.HTTP_502_BAD_GATEWAY
        return JSONResponse(
            status_code=code,
            content=RequestResponse(
                success=False, message=exc.detail or str(exc)
            ).model_dump(),
        )

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.GRAB,
            detail={
                "kind": "request",
                "action_id": action.id,
                "action_name": action.name,
                "tmdb_id": body.tmdb_id,
                "type": body.type,
                "seasons": body.seasons,
                "seerr_request_id": result.id,
                "success": True,
            },
        )
    )

    return RequestResponse(success=True, request_id=result.id)
