"""Deciding who gets a notification, and sending it.

The rules live here rather than at each emitter, so that adding a fourth place
that raises a notification cannot accidentally ship with a different idea of
who should hear about it:

* notifications have to be switched on at all — this is the master toggle, off
  by default, and until an admin turns it on this is where everything stops;
* the type has to be switched on (:mod:`cplus_service.notify.prefs`);
* a relay API key has to be configured;
* **the person who caused the event never hears about it.**  An admin grabbing
  a release does not need their phone to tell them they just grabbed a
  release, and a notification that fires on your own tap is the fastest way to
  get push switched off entirely.

Delivery runs after the response, as a background task, and owns its own
database session.  A push is never on the critical path of the user's request:
the relay being slow must not make a grab slow, and the relay being down must
not make a grab fail.  For the same reason every failure here is logged and
swallowed — the event already happened, and there is nothing the caller could
do about it.

Who counts as an admin is decided at registration, not here: a device row only
exists because someone passed the ADMIN check at
``POST /manager/push-devices``.  If that stops being true for them later, the
device is removed from the Notifications tab; we do not re-check Seerr on every
push, which would put an outbound call back on a path that exists to avoid one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import ApnsDevice
from ..db.session import get_config
from .messages import Notification
from .prefs import is_enabled
from .relay import RelayClient, RelaySettings, SendOutcome

logger = logging.getLogger(__name__)

#: What the Notifications tab says when the master switch is off.  Kept here
#: rather than at the call site so the emitting path and the "send a test"
#: button cannot drift into explaining the same state two different ways.
DISABLED_REASON = (
    "Notifications are switched off for this instance. Turn them on at the top "
    "of the Notifications tab."
)

UNCONFIGURED_REASON = (
    "No relay API key is set. Notifications are on, but there is nowhere to "
    "send them until a key is saved."
)


@dataclass(frozen=True)
class DispatchReport:
    """What one dispatch did.

    Returned for tests and for the admin UI's "send a test notification"
    button, which needs to tell an admin *why* nothing arrived. Nothing on the
    emitting path reads it.
    """

    delivered: int = 0
    failed: int = 0
    unregistered: int = 0
    """Devices Apple reported as dead, by way of the relay. They have been
    deleted. The relay stores no device tokens and so cannot do this itself —
    if this side ignores the outcome, nothing else cleans up after it."""

    skipped_reason: str | None = None
    """Set when no push was attempted at all: notifications are off, the type
    is off, no relay key is set, or there are no devices."""

    @property
    def attempted(self) -> bool:
        return self.skipped_reason is None


async def eligible_devices(
    session: AsyncSession, *, exclude_user_id: int | None = None
) -> list[ApnsDevice]:
    """Every registered device that should receive a notification.

    ``exclude_user_id`` is how "you are not told about your own actions" is
    enforced — by owner rather than by device, so an admin with a phone and an
    Apple TV is silent on both when it was them who acted.
    """
    query = select(ApnsDevice).order_by(ApnsDevice.created_at)
    if exclude_user_id is not None:
        query = query.where(ApnsDevice.user_id != exclude_user_id)
    return list((await session.execute(query)).scalars().all())


async def send_to_devices(
    session: AsyncSession,
    *,
    notification: Notification,
    devices: list[ApnsDevice],
    settings: RelaySettings,
    http: httpx.AsyncClient,
) -> DispatchReport:
    """Push to each device in turn, deleting the ones Apple says are gone.

    Sequential rather than gathered: an install has a handful of admin devices,
    not thousands, and sending them one at a time keeps a burst from tripping
    the relay's per-instance rate limit for no gain worth measuring.
    """
    client = RelayClient(settings, client=http)

    delivered = failed = unregistered = 0
    for device in devices:
        result = await client.send(
            notification,
            device_token=device.device_token,
            environment=device.environment,
        )

        if result.outcome is SendOutcome.DELIVERED:
            delivered += 1
        elif result.outcome is SendOutcome.UNREGISTERED:
            unregistered += 1
            logger.info(
                "dropping APNs device %s...: %s",
                device.device_token[:8],
                result.reason or "unregistered",
            )
            await session.delete(device)
        else:
            failed += 1

    await session.flush()
    return DispatchReport(delivered=delivered, failed=failed, unregistered=unregistered)


async def deliver(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    notification: Notification,
    exclude_user_id: int | None = None,
) -> DispatchReport:
    """Apply the rules above and send. Opens and commits its own session.

    Never raises: this runs after the response has gone out, so there is no
    caller left to handle anything.
    """
    try:
        async with sessionmaker() as session:
            try:
                report = await _deliver_in_session(
                    session,
                    http=http,
                    notification=notification,
                    exclude_user_id=exclude_user_id,
                )
                await session.commit()
                return report
            except Exception:
                await session.rollback()
                raise
    except Exception:  # pragma: no cover - defensive; the event already happened
        logger.exception("failed to deliver a %s notification", notification.type.value)
        return DispatchReport(skipped_reason="delivery raised")


async def _deliver_in_session(
    session: AsyncSession,
    *,
    http: httpx.AsyncClient,
    notification: Notification,
    exclude_user_id: int | None,
) -> DispatchReport:
    config = await get_config(session)

    # The master switch first, before the per-type ones: an install with
    # notifications off should read as "off", not as "that type is off".
    if not config.notifications_enabled:
        return DispatchReport(skipped_reason=DISABLED_REASON)

    if not await is_enabled(session, notification.type):
        return DispatchReport(skipped_reason="This notification type is switched off.")

    settings = RelaySettings.from_config(config)
    if settings is None:
        return DispatchReport(skipped_reason=UNCONFIGURED_REASON)

    devices = await eligible_devices(session, exclude_user_id=exclude_user_id)
    if not devices:
        return DispatchReport(skipped_reason="No devices are registered for push.")

    return await send_to_devices(
        session,
        notification=notification,
        devices=devices,
        settings=settings,
        http=http,
    )


__all__ = [
    "DISABLED_REASON",
    "UNCONFIGURED_REASON",
    "DispatchReport",
    "deliver",
    "eligible_devices",
    "send_to_devices",
]
