"""``POST /grab`` — send a release to an action's download client."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.identity import authenticate_plex_token
from ...db.models import Action, ActivityLog, EventType, Grab, Permission, User
from ...prowlarr.client import ProwlarrError
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import (
    DbDep,
    PlexTokenDep,
    ProwlarrDep,
    SeerrDep,
    get_cached_user,
    require_request_manager,
)
from ..schemas import GrabRequest, GrabResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


async def permitted_action(session: AsyncSession, user_id: int, action_id: int) -> Action:
    """Load an action the user is actually allowed to use.

    A missing action and an unpermitted one both return 403 — telling an
    unauthorised caller which action ids exist would be a needless disclosure.
    """
    result = await session.execute(
        select(Action)
        .join(Permission, Permission.action_id == Action.id)
        .where(Permission.user_id == user_id, Action.id == action_id)
    )
    action = result.scalars().first()
    if action is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You do not have permission to use that action"
        )
    return action


async def _resolve_target(
    db: DbDep, seerr, plex_token: str, body: GrabRequest
) -> tuple[User, Action | None, int]:
    """Resolve the caller and the download client the grab should go to.

    Two paths, chosen by which field the body carries:

    * ``action_id`` — tvOS. Cache-only auth, and the action must be one the
      caller was granted.
    * ``download_client_id`` — the admin app. Actions are a tvOS concept (a
      button label and a recommendation), so an admin choosing a specific
      release during an approval names the client directly. Needs the caller's
      Seerr permissions, which the stored mapping does not hold, so this path
      validates live.
    """
    if body.download_client_id is not None:
        try:
            user, auth = await authenticate_plex_token(db, seerr, plex_token)
        except SeerrAuthError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                exc.detail or "Seerr rejected this Plex token",
            ) from exc
        except SeerrError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
            ) from exc

        require_request_manager(auth)
        return user, None, body.download_client_id

    # The same cache-only resolution ``/search`` gets from ``CachedUserDep``.
    # Called directly rather than declared as a dependency because it applies to
    # only one of this route's two paths.
    user = await get_cached_user(db, plex_token)

    action = await permitted_action(db, user.id, int(body.action_id or 0))
    if action.is_system:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{action.name}' is not a Prowlarr action. Use POST /request instead.",
        )
    if action.download_client_id is None:  # pragma: no cover - see below
        # Unreachable as the schema stands: ``ck_action_targets_required_unless_system``
        # lets only a system action omit a download client, and those are turned
        # away above. Kept as the safety net if that constraint is ever relaxed,
        # since grabbing with no client would otherwise fail deep inside Prowlarr.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Action '{action.name}' has no download client configured.",
        )
    return user, action, action.download_client_id


@router.post("/grab", response_model=GrabResponse)
async def grab(
    db: DbDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    body: GrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release through Prowlarr.

    See :class:`~cplus_service.api.schemas.GrabRequest` for the two ways to
    name the download client, and who may use each.
    """
    user, action, download_client_id = await _resolve_target(db, seerr, plex_token, body)

    try:
        await prowlarr.grab(
            guid=body.release_guid,
            indexer_id=body.indexer_id,
            download_client_id=download_client_id,
        )
    except ProwlarrError as exc:
        logger.warning(
            "grab failed for user=%s guid=%s: %s", user.id, body.release_guid, exc
        )
        db.add(
            ActivityLog(
                user_id=user.id,
                event_type=EventType.GRAB,
                detail={
                    "action_id": action.id if action else None,
                    "download_client_id": download_client_id,
                    "release_guid": body.release_guid,
                    "indexer_id": body.indexer_id,
                    "success": False,
                    "error": str(exc),
                },
            )
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=GrabResponse(
                success=False, message=f"Prowlarr rejected the grab: {exc}"
            ).model_dump(),
        )

    record = Grab(
        user_id=user.id,
        action_id=action.id if action else None,
        release_title=body.release_title,
        release_guid=body.release_guid,
        indexer_id=body.indexer_id,
        size_bytes=body.size_bytes,
    )
    db.add(record)
    await db.flush()

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.GRAB,
            detail={
                "action_id": action.id if action else None,
                "action_name": action.name if action else None,
                "download_client_id": download_client_id,
                "release_guid": body.release_guid,
                "release_title": body.release_title,
                "indexer_id": body.indexer_id,
                "size_bytes": body.size_bytes,
                "success": True,
            },
        )
    )

    return GrabResponse(success=True, grab_id=record.id)


@router.get("/download-clients")
async def list_download_clients(
    db: DbDep, prowlarr: ProwlarrDep, seerr: SeerrDep, plex_token: PlexTokenDep
) -> Any:
    """Prowlarr's download clients, for the admin app's grab picker.

    The web UI has its own session-gated copy of this; the admin app
    authenticates with a Plex token instead, so it needs one of its own. Same
    gate as an action-free grab — if you cannot grab directly, knowing the
    client list is no use to you.
    """
    try:
        _, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    try:
        clients = await prowlarr.list_download_clients()
    except ProwlarrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Prowlarr: {exc}"
        ) from exc

    return {
        "download_clients": [
            {"id": c.id, "name": c.name, "enable": c.enable, "protocol": c.protocol}
            for c in clients
        ]
    }
