"""The part of grabbing a release that every caller shares.

``POST /grab`` (tvOS, action-scoped, cache-only auth) and ``POST /manager/grab``
(the admin app's action-free grab, live Seerr auth) differ entirely in *who* is
calling and *which* download client to send to. Once that is resolved, sending
the release to Prowlarr and writing the ``grabs``/activity-log rows is
identical — that shared tail lives here so neither route repeats it.
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Action, ActivityLog, EventType, Grab, User
from ..notify.messages import media_from_release_title, user_action
from ..prowlarr.client import ProwlarrClient, ProwlarrError
from .notifications import media_of, schedule
from .schemas import GrabResponse, ReleaseFields
from .state import AppState

logger = logging.getLogger(__name__)


async def execute_grab(
    db: AsyncSession,
    prowlarr: ProwlarrClient,
    *,
    user: User,
    action: Action | None,
    download_client_id: int,
    body: ReleaseFields,
    state: AppState,
    background: BackgroundTasks,
) -> GrabResponse | JSONResponse:
    """Send ``body``'s release to ``download_client_id`` and record the outcome.

    ``action`` is ``None`` for the admin app's action-free grab; the ``grabs``
    row's ``action_id`` is nullable for exactly that reason. It is also what
    decides whether this raises a notification — see below.
    """
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

    # Only an action-backed grab is "a user performed an action". The other
    # caller here is the admin app's action-free grab, which is an admin
    # picking a release during a request approval — their own work, and not
    # something to notify them about. ``exclude_user_id`` covers the remaining
    # case: an admin who also holds actions and used one from tvOS.
    if action is not None:
        schedule(
            background,
            state,
            user_action(
                media_of(body, fallback=media_from_release_title(body.release_title)),
                username=user.plex_username,
                action_name=action.name,
                grab_id=record.id,
                action_id=action.id,
            ),
            exclude_user_id=user.id,
        )

    return GrabResponse(success=True, grab_id=record.id)
