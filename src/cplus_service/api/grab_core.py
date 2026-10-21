"""The part of grabbing a release that every caller shares.

``POST /grab`` (tvOS, action-scoped, cache-only auth) and ``POST /manager/grab``
(the admin app's action-free grab, live Seerr auth) differ entirely in *who* is
calling and *which* download client to send to. Once that is resolved, sending
the release to Prowlarr and writing the ``grabs``/activity-log rows is
identical — that shared tail lives here so neither route repeats it.

So does starting grab tracking (see :mod:`cplus_service.tracking`), for the
same reason: once the grab row exists, following the download to Plex does not
depend on who asked for it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from fastapi import BackgroundTasks, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Action, ActivityLog, EventType, Grab, User
from ..db.session import get_config
from ..notify.messages import media_from_release_title, user_action
from ..prowlarr.client import ProwlarrClient, ProwlarrError
from ..release.infohash import info_hash_from_magnet, info_hash_of, normalise_info_hash
from ..tracking.hashes import ReleaseHashCache
from ..tracking.runner import TrackingSettings
from ..tracking.state import GrabPhase
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

    # Before the grab is sent: grab tracking looks for a torrent qBittorrent
    # added after (about) this moment.
    grabbed_at = time.time()
    try:
        result = await prowlarr.grab(
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

    # Strictly after Prowlarr has accepted the grab, and unable to change its
    # outcome: starting tracking reads the config row and nothing else — no
    # call to qBittorrent happens on this request — and any failure to start
    # leaves the grab exactly as successful as it already is.
    tracked = await start_tracking(
        db, state, record, body=body, prowlarr_echo=result.raw, grabbed_at=grabbed_at
    )
    # ``grab_id`` is what the app follows on ``GET /grabs/{id}``, so it is only
    # handed out for a grab that is actually being followed. Without it the app
    # keeps its own refresh timers. (Neither shipped client reads it today: the
    # tvOS app ignores the field, and the admin app decodes it as optional.)
    return GrabResponse(success=True, grab_id=record.id if tracked else None)


def hash_hints(
    body: ReleaseFields, prowlarr_echo: Mapping[str, Any], cache: ReleaseHashCache
) -> list[str]:
    """Every info hash this grab's torrent might answer to, best source first.

    The app's own ``info_hash`` if it sent one; the hash this service saw for
    the release in a recent search; a magnet link standing in for the guid; and
    whatever Prowlarr echoed back from the grab. Often none of them — a private
    tracker gives no hash anywhere — and then the torrent is found by name.
    """
    candidates = [
        normalise_info_hash(body.info_hash),
        cache.lookup(body.indexer_id, body.release_guid),
        info_hash_from_magnet(body.release_guid),
        info_hash_of(prowlarr_echo),
    ]
    hints: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in hints:
            hints.append(candidate)
    return hints


async def start_tracking(
    db: AsyncSession,
    state: AppState,
    record: Grab,
    *,
    body: ReleaseFields,
    prowlarr_echo: Mapping[str, Any],
    grabbed_at: float,
) -> bool:
    """Begin following ``record``'s download to Plex, if grab tracking is on.

    Returns whether tracking started. Does nothing at all when it is off or not
    fully configured, so the grab behaves exactly as it did before tracking
    existed. Never talks to qBittorrent: whether qBittorrent is reachable, and
    whether it accepts the password, is the background task's to find out, and
    it reports a failure there as that grab's ``failed`` phase.

    When it is on, the grab row is committed here, ahead of the request's own
    commit: the tracking task writes to the row from its own session, and must
    find it there.
    """
    settings = TrackingSettings.from_config(await get_config(db))
    if settings is None:
        return False
    record.tracking_phase = GrabPhase.FINDING_TORRENT.value
    await db.commit()
    try:
        _launch(state, record, settings, body=body, prowlarr_echo=prowlarr_echo,
                grabbed_at=grabbed_at)
    except Exception:
        logger.exception("grab %s: could not start tracking", record.id)
        record.tracking_phase = GrabPhase.FAILED.value
        record.tracking_error = "Tracking could not start."
        return False
    return True


def _launch(
    state: AppState,
    record: Grab,
    settings: TrackingSettings,
    *,
    body: ReleaseFields,
    prowlarr_echo: Mapping[str, Any],
    grabbed_at: float,
) -> None:
    state.grab_tracker.start(
        grab_id=record.id,
        user_id=record.user_id,
        title=record.release_title,
        hash_hints=hash_hints(body, prowlarr_echo, state.release_hashes),
        grabbed_at=grabbed_at,
        settings=settings,
        sessionmaker=state.sessionmaker,
        qbittorrent_http=state.qbittorrent_http,
        plex_http=state.http,
    )
