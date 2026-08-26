"""The one line an emitting route writes to raise a notification.

Exists so that no route has to know how :func:`cplus_service.notify.service.deliver`
is wired — which client, which session factory, which token cache — and so that
"after the response, never during it" is decided once rather than remembered at
each call site.
"""

from __future__ import annotations

from fastapi import BackgroundTasks

from ..notify.messages import MediaSummary, Notification
from ..notify.service import deliver
from .schemas import MediaIdentity
from .state import AppState


def media_of(body: MediaIdentity, *, fallback: MediaSummary) -> MediaSummary:
    """The media a notification is about: what the client sent, else ``fallback``.

    A client that sends a title is believed, year and all — including a client
    that sends a title and no year, which is a real answer ("this thing has no
    release year") and not a reason to go back to guessing.
    """
    if body.media_title and body.media_title.strip():
        return MediaSummary(title=body.media_title.strip(), year=body.media_year)
    return fallback


def schedule(
    background: BackgroundTasks,
    state: AppState,
    notification: Notification,
    *,
    exclude_user_id: int | None = None,
) -> None:
    """Queue ``notification`` for delivery once the response has been sent.

    ``exclude_user_id`` is the user who caused the event; their own devices are
    skipped. Pass it always — an emitter that omits it is one that notifies an
    admin about their own tap.
    """
    background.add_task(
        deliver,
        sessionmaker=state.sessionmaker,
        http=state.relay_http,
        notification=notification,
        exclude_user_id=exclude_user_id,
    )


__all__ = ["media_of", "schedule"]
