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

``relay``
    Talking to the public forwarding service that holds the APNs signing key —
    which this install has not got and cannot get. Knows nothing about who to
    send to. Its docstring is also where the isolation argument lives: one
    instance cannot reach another's users because it has never seen their
    device tokens, not because the relay enforces a rule.

``service``
    The policy: are notifications on at all, is this type on, is a relay key
    set, who gets it, and what to do when Apple says a device is gone.
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
