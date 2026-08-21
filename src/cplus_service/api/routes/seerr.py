"""``/seerr/*`` — an allowlisted passthrough to Seerr's request API.

These exist so the Seerr admin API key stops living on every device. That is
the same problem cplus-service already solves for Prowlarr, one service over:
the client presents its Plex token, cplus exchanges it for that user's own
Seerr session, and makes the call **as them**.

cplus therefore still holds **no Seerr credential at all** — only a URL. The
session is obtained fresh per request and discarded, exactly as ``/request``
has always worked. That costs one extra Seerr round trip per call, which is
fine for these: they are user actions and badge refreshes, not paging.

Deliberately an allowlist, not a general proxy. Only these five operations are
reachable; ``/settings/*`` and everything else is not, so a caller who happens
to be the Seerr owner cannot read Radarr/Sonarr credentials back out through
this service.

Authorisation is Seerr's, with one addition. Seerr scopes ``GET /request`` by
itself — a caller without ``MANAGE_REQUESTS`` or ``REQUEST_VIEW`` sees only
their own — so the same endpoint serves the tvOS app and the admin app without
branching here. **Approve and decline are admin-only**, and cplus refuses a
non-admin itself rather than relying on Seerr's 403, so the rule is stated in
our code and not merely inherited.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from ...auth.identity import authenticate_plex_token
from ...db.models import ActivityLog, EventType
from ...seerr.client import SeerrAuthError, SeerrClient, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep, require_request_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/seerr", tags=["client"])


async def _authenticate(db: DbDep, seerr: SeerrClient, plex_token: str):  # noqa: ANN202
    """Resolve the caller to a local user and a live Seerr session.

    Live every time, like ``/request``: these calls act against Seerr as the
    user, so they need a real session rather than the stored token mapping that
    backs ``/search`` and ``/grab``.
    """
    try:
        return await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc


def _upstream_status(exc: SeerrError) -> int:
    """Keep Seerr's own 4xx; anything else is an upstream fault."""
    upstream = exc.status_code or 0
    return upstream if 400 <= upstream < 500 else status.HTTP_502_BAD_GATEWAY


def _upstream_error(exc: SeerrError) -> HTTPException:
    """Surface Seerr's own rejection."""
    return HTTPException(_upstream_status(exc), exc.detail or str(exc))


def _upstream_response(exc: SeerrError) -> JSONResponse:
    """The same rejection, as a returned response rather than a raised one.

    Identical body shape to :func:`_upstream_error`, because raising unwinds the
    request transaction — so a handler that wants its ``activity_log`` row kept
    has to return. Anything written before a ``raise`` would be rolled back and
    the audit trail would quietly be a lie.
    """
    return JSONResponse(
        status_code=_upstream_status(exc),
        content={"detail": exc.detail or str(exc)},
    )


@router.get("/me")
async def current_user(db: DbDep, seerr: SeerrDep, plex_token: PlexTokenDep) -> Any:
    """The caller's Seerr user, verbatim — the client needs its Seerr user id."""
    _, auth = await _authenticate(db, seerr, plex_token)
    try:
        return await seerr.get_current_user(session_cookie=auth.session_cookie)
    except SeerrError as exc:
        raise _upstream_error(exc) from exc


@router.get("/requests")
async def list_requests(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    take: int = Query(default=50, ge=1, le=200),
    skip: int = Query(default=0, ge=0),
    filter: str | None = Query(default=None),
    sort: str | None = Query(default=None),
) -> Any:
    """List requests, scoped by Seerr to what the caller may see.

    The admin app gets every request; a tvOS user gets their own. Same endpoint,
    no branching — Seerr's own permission check does the work.
    """
    _, auth = await _authenticate(db, seerr, plex_token)
    try:
        return await seerr.list_requests(
            session_cookie=auth.session_cookie,
            take=take,
            skip=skip,
            filter=filter,
            sort=sort,
        )
    except SeerrError as exc:
        raise _upstream_error(exc) from exc


@router.post("/requests/{request_id}/{decision}")
async def decide_request(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    request_id: int,
    decision: Literal["approve", "decline"],
) -> Any:
    """Approve or decline a request. **Admin only.**"""
    user, auth = await _authenticate(db, seerr, plex_token)
    require_request_manager(auth)

    try:
        result = await seerr.update_request_status(
            session_cookie=auth.session_cookie, request_id=request_id, status=decision
        )
    except SeerrError as exc:
        db.add(
            ActivityLog(
                user_id=user.id,
                event_type=EventType.GRAB,
                detail={
                    "kind": f"request_{decision}",
                    "seerr_request_id": request_id,
                    "success": False,
                    "error": exc.detail or str(exc),
                },
            )
        )
        # Returned, not raised, so the row above survives the request.
        return _upstream_response(exc)

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.GRAB,
            detail={
                "kind": f"request_{decision}",
                "seerr_request_id": request_id,
                "success": True,
            },
        )
    )
    return result


@router.delete("/requests/{request_id}")
async def delete_request(
    db: DbDep, seerr: SeerrDep, plex_token: PlexTokenDep, request_id: int
) -> Response:
    """Delete a request.

    Not gated here: Seerr lets a user delete their own and an admin delete any,
    which is the rule we want, and it enforces it inline rather than by
    middleware.
    """
    user, auth = await _authenticate(db, seerr, plex_token)

    try:
        await seerr.delete_request(
            session_cookie=auth.session_cookie, request_id=request_id
        )
    except SeerrError as exc:
        raise _upstream_error(exc) from exc

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.GRAB,
            detail={
                "kind": "request_delete",
                "seerr_request_id": request_id,
                "success": True,
            },
        )
    )
    return JSONResponse({"success": True})
