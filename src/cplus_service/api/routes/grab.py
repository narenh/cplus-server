"""``POST /grab`` — tvOS sends an action's recommended (or hand-picked) release
to that action's download client.

Cache-only auth throughout: the caller must already hold a mapping written by
``GET /actions``, and must have been granted the named action. The admin app's
action-free grab — naming a download client directly during a request
approval, checked against Seerr live — is a different caller entirely and
lives at ``POST /manager/grab`` instead.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Action, Permission
from ..deps import CachedUserDep, DbDep, ProwlarrDep
from ..grab_core import execute_grab
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
    db: DbDep,
    prowlarr: ProwlarrDep,
    user: CachedUserDep,
    body: GrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release through Prowlarr, on behalf of one of the caller's actions."""
    action = await permitted_action(db, user.id, body.action_id)
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

    return await execute_grab(
        db,
        prowlarr,
        user=user,
        action=action,
        download_client_id=action.download_client_id,
        body=body,
    )
