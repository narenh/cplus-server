"""``POST /playback/events`` — Canopy+'s player reporting what it is doing.

Called on every start, pause, resume and stop, and on every chapter change and
intro or credits marker — often enough that it is authenticated from the stored
Plex-token mapping (:data:`~cplus_service.api.deps.CachedUserDep`) rather than
by asking Seerr, and that it answers before doing anything slow.

The handler's whole job is: check the switch, validate, record the event in the
in-memory log, queue delivery, answer 202. Delivery to the admin's webhooks
happens after the response has gone out (``BackgroundTasks``), so the Apple TV
never waits on a light bulb's hub, and a webhook that fails is logged and shown
on the Playback tab rather than reported back to an app that could do nothing
about it. See :mod:`cplus_service.playback`.

**403 while switched off**, with a detail that says so. Canopy+ checks
``GET /capabilities`` and should not be sending at all then, so a 403 means it
has a stale answer; it treats one as "off" and logs it. Not 404 — the endpoint
exists — and not 503, which would invite a retry.

Because FastAPI resolves dependencies and the body before the handler runs, an
unregistered token is a 401 and a malformed body is a 422 even while the feature
is off. Both are more useful to the app's developer than a blanket 403.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import select

from ...db.models import PlaybackWebhook
from ...db.session import get_config
from ...playback.events import PlaybackEvent, outbound_payload
from ...playback.forward import WebhookTarget, deliver
from ..deps import CachedUserDep, DbDep, ProfileDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playback", tags=["client"])

#: What the app is told while the switch is off. Names where to turn it on,
#: since the person reading it in Canopy+'s log is usually also the admin.
DISABLED_DETAIL = (
    "Playback events are switched off on this server. An admin can turn them on"
    " in the Playback tab of the web console."
)


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def playback_event(
    body: PlaybackEvent,
    user: CachedUserDep,
    profile: ProfileDep,
    db: DbDep,
    state: StateDep,
    background: BackgroundTasks,
) -> dict[str, bool]:
    """Accept one playback event and forward it to every configured webhook."""
    config = await get_config(db)
    if not config.playback_events_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, DISABLED_DETAIL)

    received_at = datetime.now(UTC)
    payload = outbound_payload(
        body, user_name=user.plex_username, profile=profile, received_at=received_at
    )
    # Recorded whether or not there is anywhere to send it: an admin setting
    # this up wants to see the app's events arrive before they have written the
    # automation that will receive them.
    entry = state.playback.record(payload, received_at)

    # Read now, while the request's session is open; delivery runs after it
    # has closed, so it gets plain values rather than ORM rows.
    rows = await db.execute(select(PlaybackWebhook).order_by(PlaybackWebhook.id))
    targets = [WebhookTarget(id=row.id, url=row.url) for row in rows.scalars()]
    if targets:
        background.add_task(
            deliver, state.playback_http, targets, payload, state.playback, entry=entry
        )
    else:
        entry.deliveries = []

    logger.debug(
        "playback %s from %s (%d webhook%s)",
        body.event.value,
        body.room or "an unnamed room",
        len(targets),
        "" if len(targets) == 1 else "s",
    )
    return {"accepted": True}
