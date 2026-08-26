"""The one line an emitting route writes to raise a notification.

Exists so that no route has to know how :func:`cplus_service.notify.service.deliver`
is wired — which client, which session factory, which token cache — and so that
"after the response, never during it" is decided once rather than remembered at
each call site.
"""

from __future__ import annotations

from fastapi import BackgroundTasks

from ..notify.messages import Notification
from ..notify.service import deliver
from .state import AppState


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
        http=state.apns_http,
        tokens=state.apns_tokens,
        notification=notification,
        exclude_user_id=exclude_user_id,
    )


__all__ = ["schedule"]
