"""Admin push notifications.

Layered so that each piece can be read and tested on its own:

``types``
    The catalogue of what an admin can be notified about. Adding a type starts
    here.

``messages``
    Rendering one event into the title/subtitle pair a notification shows.
    Pure functions, no transport vocabulary.

``prefs``
    The per-type switches, and the "unset means enabled" rule that makes a new
    type live on existing installs without a backfill.

``apns``
    Talking to Apple: provider-token signing, the HTTP/2 push, and reading a
    dead device token out of the response. Knows nothing about who to send to.

``service``
    The policy: is this type on, is push configured, who gets it, and what to
    do when Apple says a device is gone.
"""

from .messages import MediaSummary, Notification, user_action, user_requested
from .types import NOTIFICATION_TYPES, NotificationType

__all__ = [
    "NOTIFICATION_TYPES",
    "MediaSummary",
    "Notification",
    "NotificationType",
    "user_action",
    "user_requested",
]
