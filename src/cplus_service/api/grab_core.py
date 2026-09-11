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
    download_client_id: int | None,
    body: ReleaseFields,
    state: AppState,
    background: BackgroundTasks,
) -> GrabResponse | JSONResponse:
    """Send ``body``'s release to ``download_client_id`` and record the outcome.

    ``download_client_id`` is ``None`` when the caller did not name one, which
    asks Prowlarr for its own default. An action always names one; the admin
    app's direct grab may, and Canopy+'s "More Versions" does not — it is
    replacing a button that never picked a client either.

    ``action`` is ``None`` for the admin app's action-free grab; the ``grabs``
    row's ``action_id`` is nullable for exactly that reason, and its
    ``via_manager`` records which of the two reasons applies here. It is also
    what decides whether this raises a notification — see below.
    """
    # An action-free grab is the admin app's own work, not a user exercising an
    # action, so it is filed under ADMIN (with a ``kind``) rather than GRAB. The
    # two would otherwise be indistinguishable in the activity log: both write a
    # ``grabs`` row and a grab event, and the only tell was a null ``action_id``,
    # which is also what a grab whose action was later deleted looks like.
    admin = action is None
    event_type = EventType.ADMIN if admin else EventType.GRAB
    kind = {"kind": "grab"} if admin else {}

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
                event_type=event_type,
                detail={
                    **kind,
                    "action_id": action.id if action else None,
                    "download_client_id": download_client_id,
                    "release_title": body.release_title,
                    "release_guid": body.release_guid,
                    "indexer_id": body.indexer_id,
                    "success": False,
                    # The diagnostic message, not ``summary``: this one is read
                    # on the admin's own activity page, by the person who
                    # configured Prowlarr and can act on what it said.
                    "error": str(exc),
                },
            )
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=GrabResponse(
                success=False, message=f"Prowlarr rejected the grab. {exc.summary}"
            ).model_dump(),
        )

    record = Grab(
        user_id=user.id,
        action_id=action.id if action else None,
        via_manager=admin,
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
            event_type=event_type,
            detail={
                **kind,
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
