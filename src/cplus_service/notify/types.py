"""The notification catalogue: what an admin can be told about.

Adding a type is meant to be a one-line change here plus an emitter at the
place the thing happens.  Everything downstream — the admin UI's list of
switches, the default-on behaviour, the preference storage — is driven off
:data:`NOTIFICATION_TYPES` and needs no edit.

Two rules keep that true:

* the enum *value* is the stored key, so renaming a label is free but changing
  a value is a data migration;
* a type with no stored preference row is enabled.  New types therefore arrive
  switched on for existing installs, which is what "both enabled by default"
  asks for without seeding anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..web.copy_strings import text


class NotificationType(StrEnum):
    """A kind of event an admin can be notified about."""

    USER_REQUESTED = "user_requested"
    """A user filed a request — through the built-in Request action, or in
    Seerr itself if this install has Seerr's webhook pointed at it."""

    USER_ACTION = "user_action"
    """A user ran one of the admin's Prowlarr-backed actions on a release."""


@dataclass(frozen=True)
class NotificationTypeInfo:
    """How one type is described to an admin, and what it looks like on screen.

    ``example_subtitle`` is not decoration: the switch list is the only place
    an admin sees the shape of a notification before one arrives, and the two
    types differ only in their second line.
    """

    type: NotificationType
    label: str
    description: str
    example_subtitle: str


NOTIFICATION_TYPES: tuple[NotificationTypeInfo, ...] = (
    NotificationTypeInfo(
        type=NotificationType.USER_REQUESTED,
        label=text("py_notify.type_user_requested.label"),
        description=text("py_notify.type_user_requested.description"),
        example_subtitle=text("py_notify.type_user_requested.example_subtitle"),
    ),
    NotificationTypeInfo(
        type=NotificationType.USER_ACTION,
        label=text("py_notify.type_user_action.label"),
        description=text("py_notify.type_user_action.description"),
        example_subtitle=text("py_notify.type_user_action.example_subtitle"),
    ),
)

#: Lookup by stored key, for rendering a preference row whose type is known.
NOTIFICATION_TYPES_BY_VALUE: dict[str, NotificationTypeInfo] = {
    info.type.value: info for info in NOTIFICATION_TYPES
}


__all__ = [
    "NOTIFICATION_TYPES",
    "NOTIFICATION_TYPES_BY_VALUE",
    "NotificationType",
    "NotificationTypeInfo",
]
