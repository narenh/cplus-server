"""Reading and writing the admin's per-type notification switches.

The storage rule this module exists to enforce, stated once: **a type with no
row is enabled.**  Nothing outside here should look at
``notification_preferences`` directly, or that default stops being true the
first time someone writes ``select(...).where(enabled.is_(True))`` and gets an
empty list back on a fresh install.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import NotificationPreference
from .types import NOTIFICATION_TYPES, NotificationType

#: What an unknown or unset type resolves to.  Changing this changes the
#: meaning of every install that has never touched the switches.
DEFAULT_ENABLED = True


async def _stored(session: AsyncSession) -> dict[str, bool]:
    rows = (await session.execute(select(NotificationPreference))).scalars().all()
    return {row.notification_type: row.enabled for row in rows}


async def is_enabled(session: AsyncSession, notification_type: NotificationType) -> bool:
    """Whether ``notification_type`` should currently be delivered."""
    row = await session.get(NotificationPreference, notification_type.value)
    if row is None:
        return DEFAULT_ENABLED
    return row.enabled


async def current(session: AsyncSession) -> dict[NotificationType, bool]:
    """Every known type's effective setting, including the ones never stored.

    Keyed by the enum rather than the raw string, and covering exactly the
    types this version knows about — a leftover row for a type that no longer
    exists is ignored rather than surfaced as a switch nobody can explain.
    """
    stored = await _stored(session)
    return {
        info.type: stored.get(info.type.value, DEFAULT_ENABLED) for info in NOTIFICATION_TYPES
    }


async def set_enabled(
    session: AsyncSession, notification_type: NotificationType, enabled: bool
) -> bool:
    """Turn one type on or off. Idempotent; returns the value now in effect.

    A row is written even when the value matches the default, so that "on"
    chosen deliberately and "on" by never having been touched are the same
    thing to every reader — which they are, and which is what keeps a future
    change to :data:`DEFAULT_ENABLED` from silently rewriting an admin's
    explicit choice.
    """
    row = await session.get(NotificationPreference, notification_type.value)
    if row is None:
        row = NotificationPreference(
            notification_type=notification_type.value, enabled=enabled
        )
        session.add(row)
    else:
        row.enabled = enabled
    await session.flush()
    return enabled


__all__ = ["DEFAULT_ENABLED", "current", "is_enabled", "set_enabled"]
