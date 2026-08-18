"""``POST /grab`` — send a release to an action's download client."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Action, ActivityLog, EventType, Grab, Permission
from ...prowlarr.client import ProwlarrError
from ..deps import CachedUserDep, DbDep, ProwlarrDep, StateDep
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


@router.post("/grab", response_model=GrabResponse)
async def grab(
    state: StateDep,
    db: DbDep,
    prowlarr: ProwlarrDep,
    user: CachedUserDep,
    body: GrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release through Prowlarr using the action's download client.

    Authenticated from the Plex-token cache only.
    """
    action = await permitted_action(db, user.user_id, body.action_id)

    if action.is_system:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{action.name}' is not a Prowlarr action. Use POST /request instead.",
        )
    if action.download_client_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Action '{action.name}' has no download client configured.",
        )

    # Enrichment only: the title and size make the history readable, and their
    # absence must not stop the grab.
    cached = await state.release_cache.get(body.release_guid)

    try:
        await prowlarr.grab(
            guid=body.release_guid,
            indexer_id=body.indexer_id,
            download_client_id=action.download_client_id,
        )
    except ProwlarrError as exc:
        logger.warning("grab failed for user=%s guid=%s: %s", user.user_id, body.release_guid, exc)
        db.add(
            ActivityLog(
                user_id=user.user_id,
                event_type=EventType.GRAB,
                detail={
                    "action_id": action.id,
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
        user_id=user.user_id,
        action_id=action.id,
        release_title=cached.title if cached else None,
        release_guid=body.release_guid,
        indexer_id=body.indexer_id,
        size_bytes=cached.size_bytes if cached else None,
    )
    db.add(record)
    await db.flush()

    db.add(
        ActivityLog(
            user_id=user.user_id,
            event_type=EventType.GRAB,
            detail={
                "action_id": action.id,
                "action_name": action.name,
                "release_guid": body.release_guid,
                "release_title": cached.title if cached else None,
                "indexer_id": body.indexer_id,
                "size_bytes": cached.size_bytes if cached else None,
                "success": True,
            },
        )
    )

    return GrabResponse(success=True, grab_id=record.id)
