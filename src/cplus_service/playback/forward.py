"""Forwarding playback events to the admin's webhooks, and remembering how it went.

Delivery runs after ``POST /playback/events`` has already answered, so nothing
here may raise: every failure becomes a :class:`DeliveryResult` with a reason,
a log line, and a line on the Playback tab. There are no retries. A playback
event is only worth anything within a second or two of happening — lights that
come up a minute after the credits started are a bug of their own — so a missed
delivery is better reported than repeated.

Every webhook is posted to at once, each under its own timeout, so one slow or
unreachable URL costs the others nothing.

**URLs are kept out of the log.** A Home Assistant webhook URL carries its
webhook id in the path, and that id is the whole of its authentication: anyone
who has it can fire the automation. So log lines name a webhook by scheme and
host only (:func:`redact_url`); the full URL is shown on the Playback tab,
which only an admin can open, and nowhere else.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .events import describe_item

logger = logging.getLogger(__name__)

#: How long one webhook gets, end to end, before it is given up on. Short,
#: because a home-automation endpoint on the same network answers in
#: milliseconds, and an answer that takes longer than this is arriving too late
#: to dim anything at the right moment anyway.
DELIVERY_TIMEOUT_SECONDS = 5.0

#: How many events the Playback tab's recent list keeps. Enough to watch a
#: whole film's worth of chapter changes go by while writing an automation;
#: not so many that the list is a history nobody asked this service to keep.
RECENT_EVENT_LIMIT = 50

#: How much of a failure's own text is kept. An exception message is normally a
#: line; this keeps a pathological one from filling the page.
MAX_ERROR_LENGTH = 200


@dataclass(frozen=True)
class WebhookTarget:
    """One URL to deliver to, detached from the database session it came from.

    Delivery runs after the request's session has closed, so it is handed plain
    values rather than ORM rows that would try to lazy-load from a dead session.
    """

    id: int
    url: str


@dataclass
class DeliveryResult:
    """How one delivery to one webhook went."""

    webhook_id: int
    ok: bool
    at: datetime
    event: str
    status_code: int | None = None
    error: str | None = None

    @property
    def summary(self) -> str:
        """The result in a few words, for the page."""
        if self.status_code is not None:
            return f"HTTP {self.status_code}"
        return self.error or "failed"


@dataclass
class RecentEvent:
    """One event as the Playback tab's recent list shows it.

    Keeps the forwarded body itself, so the list shows exactly what the
    webhooks were sent, and ``deliveries`` once forwarding has finished —
    ``None`` until then, which is a real state for the second or so it lasts.
    """

    payload: dict[str, Any]
    received_at: datetime
    deliveries: list[DeliveryResult] | None = None

    @property
    def event(self) -> str:
        return str(self.payload.get("event", ""))

    @property
    def room(self) -> str | None:
        return self.payload.get("room")

    @property
    def user(self) -> str:
        return str((self.payload.get("user") or {}).get("name") or "")

    @property
    def title(self) -> str:
        return describe_item(self.payload.get("item") or {})

    @property
    def chapter_title(self) -> str | None:
        chapter = self.payload.get("chapter") or {}
        if not chapter:
            return None
        return chapter.get("title") or f"Chapter {chapter.get('index', 0) + 1}"

    @property
    def watched(self) -> bool | None:
        return self.payload.get("watched")

    @property
    def test(self) -> bool:
        return bool(self.payload.get("test"))

    @property
    def delivered(self) -> int:
        return sum(1 for result in self.deliveries or [] if result.ok)


@dataclass
class PlaybackLog:
    """Recent events and each webhook's last result, in memory only.

    Hung off :class:`~cplus_service.api.state.AppState`, so it lasts exactly as
    long as the process does. Not shared between workers — this service runs
    one — and needs no lock: every reader and writer is on the one event loop.
    """

    recent: deque[RecentEvent] = field(
        default_factory=lambda: deque(maxlen=RECENT_EVENT_LIMIT)
    )
    """Newest first, so the page can render it in order without reversing."""

    last_results: dict[int, DeliveryResult] = field(default_factory=dict)
    """Keyed by :class:`~cplus_service.db.models.PlaybackWebhook` id."""

    def record(self, payload: dict[str, Any], received_at: datetime) -> RecentEvent:
        entry = RecentEvent(payload=payload, received_at=received_at)
        self.recent.appendleft(entry)
        return entry

    def forget_webhook(self, webhook_id: int) -> None:
        self.last_results.pop(webhook_id, None)


def redact_url(url: str) -> str:
    """A webhook URL with everything after the host replaced, for log lines.

    ``http://homeassistant.local:8123/api/webhook/<secret>`` becomes
    ``http://homeassistant.local:8123/…`` — enough to tell which of an admin's
    webhooks a line is about, and not enough to fire it.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable URL)"
    if not parts.scheme or not parts.hostname:
        return "(unparseable URL)"
    host = parts.hostname
    if ":" in host:  # an IPv6 literal goes back into its brackets
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}/…"


async def deliver(
    http: httpx.AsyncClient,
    targets: Sequence[WebhookTarget],
    payload: dict[str, Any],
    log: PlaybackLog,
    *,
    entry: RecentEvent | None = None,
) -> list[DeliveryResult]:
    """POST ``payload`` to every target at once. Never raises.

    Results are recorded on ``log`` as each webhook's last result and, when
    ``entry`` is given, on that recent-list entry too. Returned in the order of
    ``targets``, for the test button, which shows them.
    """
    results = list(
        await asyncio.gather(*(_deliver_one(http, target, payload) for target in targets))
    )
    for result in results:
        log.last_results[result.webhook_id] = result
    if entry is not None:
        entry.deliveries = results
    return results


async def _deliver_one(
    http: httpx.AsyncClient, target: WebhookTarget, payload: dict[str, Any]
) -> DeliveryResult:
    """One webhook, one attempt.

    The ``asyncio.timeout`` is the real bound. httpx's own timeouts are per
    phase — connect, then each read — so an endpoint that trickles a byte every
    few seconds could hold a delivery open for far longer than any one of them.

    Anything at all that goes wrong is caught, not just ``httpx.HTTPError``: this
    runs where nobody is waiting to handle an exception, and an unexpected one
    would only end up as an unretrieved-task warning that says nothing useful.
    """
    event = str(payload.get("event", ""))
    at = datetime.now(UTC)
    try:
        async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
            response = await http.post(target.url, json=payload)
    except (TimeoutError, httpx.TimeoutException):
        error = f"Timed out after {DELIVERY_TIMEOUT_SECONDS:g} s"
    except httpx.ConnectError as exc:
        error = _short(f"Could not connect: {exc}" if str(exc) else "Could not connect")
    except Exception as exc:  # deliberately broad; see the docstring
        error = _short(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
    else:
        ok = response.is_success
        if not ok:
            logger.warning(
                "playback webhook %s answered %s to a %s event",
                redact_url(target.url),
                response.status_code,
                event,
            )
        return DeliveryResult(
            webhook_id=target.id, ok=ok, at=at, event=event, status_code=response.status_code
        )

    logger.warning(
        "playback webhook %s failed for a %s event: %s", redact_url(target.url), event, error
    )
    return DeliveryResult(webhook_id=target.id, ok=False, at=at, event=event, error=error)


def _short(text: str) -> str:
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 1] + "…"
