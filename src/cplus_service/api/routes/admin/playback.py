"""The Playback tab: the master switch, the webhook URLs, and what has arrived.

A page of its own rather than a section of Configuration, because it is used
differently: Configuration is visited once, while this is the page an admin
keeps open beside Home Assistant while writing an automation, pressing "Send
test event" and watching events from the living room arrive. So it carries the
in-memory recent list, refreshed every few seconds, and the payload
documentation — the two things that admin needs in view at once.

Every write lands in place, in the style of the rest of the console: the switch
returns itself, and adding, removing or testing a webhook returns the webhook
card. A refused URL is a 400 whose detail the page's error toast shows.

The test button works whether or not the master switch is on. The switch
governs whether the *app* sends; an admin pressing a button that says "send"
has asked for exactly one delivery, and setting up the receiving end before
turning the app loose on it is the natural order to do things in.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import PlaybackWebhook, User
from ....db.session import get_config
from ....playback.events import (
    PlaybackEventKind,
    outbound_payload,
    sample_event,
)
from ....playback.forward import (
    DELIVERY_TIMEOUT_SECONDS,
    RECENT_EVENT_LIMIT,
    DeliveryResult,
    PlaybackLog,
    WebhookTarget,
    deliver,
    redact_url,
)
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playback", tags=["admin"])

#: The column's own width. A URL longer than this is not a webhook anyone typed.
MAX_URL_LENGTH = 2048

#: What each event means, in the order the page lists them. Kept beside the
#: enum's values rather than in the template so a new event cannot be added to
#: the schema without this dictionary visibly lacking it (the page test checks
#: every event is described).
EVENT_DESCRIPTIONS: dict[PlaybackEventKind, str] = {
    PlaybackEventKind.STARTED: "Playback began, from the start or resuming a title.",
    PlaybackEventKind.PAUSED: "Paused.",
    PlaybackEventKind.RESUMED: "Resumed after a pause.",
    PlaybackEventKind.STOPPED: (
        "The player closed. watched is true if the title was played to the end"
        " or left past 90%."
    ),
    PlaybackEventKind.CHAPTER: "A new chapter began; chapter says which.",
    PlaybackEventKind.CREDITS_STARTED: "The credits marker was reached.",
    PlaybackEventKind.INTRO_STARTED: "The intro marker began.",
    PlaybackEventKind.INTRO_ENDED: "The intro marker ended, or was skipped.",
}


async def _webhooks(db: AsyncSession) -> list[PlaybackWebhook]:
    rows = await db.execute(select(PlaybackWebhook).order_by(PlaybackWebhook.id))
    return list(rows.scalars().all())


def _webhooks_context(
    webhooks: list[PlaybackWebhook],
    log: PlaybackLog,
    *,
    test_message: str | None = None,
    test_ok: bool = False,
    include_recent: bool = False,
) -> dict[str, Any]:
    """What the webhook card renders from.

    ``include_recent`` asks the card to carry the recent list along as an
    out-of-band swap, so the test event the admin just sent appears in it at
    once rather than at the next poll.
    """
    return {
        "webhooks": webhooks,
        "results": log.last_results,
        "test_message": test_message,
        "test_ok": test_ok,
        "include_recent": include_recent,
        "recent": list(log.recent),
    }


def _sample_payload(admin: User, *, received_at: datetime) -> dict[str, Any]:
    return outbound_payload(
        sample_event(),
        user_name=admin.plex_username,
        profile="",
        received_at=received_at,
        test=True,
    )


@router.get("", response_class=HTMLResponse)
async def playback_page(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    config = await get_config(db)
    example = _sample_payload(admin, received_at=datetime.now(UTC).replace(microsecond=0))
    # The example is the real test payload with ``test`` set back to false,
    # because it is showing what an ordinary event looks like.
    example["test"] = False
    return templates.TemplateResponse(
        request,
        "playback.html",
        {
            **_webhooks_context(await _webhooks(db), state.playback),
            "config": config,
            "events": EVENT_DESCRIPTIONS,
            "example_json": json.dumps(example, indent=2),
            "recent_limit": RECENT_EVENT_LIMIT,
            "timeout": f"{DELIVERY_TIMEOUT_SECONDS:g}",
            "admin": admin,
            "title": "Playback",
            "nav": "playback",
        },
    )


@router.post("/enabled", response_class=HTMLResponse)
async def toggle_enabled(
    request: Request, db: DbDep, admin: AdminPageDep, enabled: str = Form(default="")
) -> Response:
    """Flip the master switch. Takes effect at the app's next capabilities check."""
    config = await get_config(db)
    config.playback_events_enabled = enabled == "on"
    await db.flush()
    logger.info(
        "playback events switched %s", "on" if config.playback_events_enabled else "off"
    )
    return templates.TemplateResponse(
        request, "partials/playback_switch.html", {"config": config}
    )


@router.post("/webhooks", response_class=HTMLResponse)
async def add_webhook(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    url: str = Form(default=""),
) -> Response:
    """Add one URL every event will be forwarded to."""
    cleaned = _validated_url(url)
    existing = await db.execute(select(PlaybackWebhook).where(PlaybackWebhook.url == cleaned))
    if existing.scalars().first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That webhook is already on the list.")

    db.add(PlaybackWebhook(url=cleaned))
    await db.flush()
    logger.info("added playback webhook %s", redact_url(cleaned))

    return templates.TemplateResponse(
        request,
        "partials/playback_webhooks.html",
        _webhooks_context(await _webhooks(db), state.playback),
    )


@router.post("/webhooks/{webhook_id}/delete", response_class=HTMLResponse)
async def delete_webhook(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, webhook_id: int
) -> Response:
    """Remove one URL. Removing one that is already gone is not an error."""
    webhook = await db.get(PlaybackWebhook, webhook_id)
    if webhook is not None:
        logger.info("removed playback webhook %s", redact_url(webhook.url))
        await db.delete(webhook)
        await db.flush()
    state.playback.forget_webhook(webhook_id)

    return templates.TemplateResponse(
        request,
        "partials/playback_webhooks.html",
        _webhooks_context(await _webhooks(db), state.playback),
    )


@router.post("/test", response_class=HTMLResponse)
async def send_test(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    """Forward a sample ``chapter`` event to every webhook, and wait for the answers.

    The one delivery that does wait: the admin pressed the button to see what
    happens, so the card comes back with each webhook's result beside it. That
    takes at most one timeout, since every webhook is tried at once.

    The event is marked ``"test": true`` and recorded in the recent list like
    any other, attributed to the admin pressing the button.
    """
    webhooks = await _webhooks(db)
    if not webhooks:
        return templates.TemplateResponse(
            request,
            "partials/playback_webhooks.html",
            _webhooks_context(
                webhooks, state.playback, test_message="Add a webhook URL first."
            ),
        )

    received_at = datetime.now(UTC)
    payload = _sample_payload(admin, received_at=received_at)
    entry = state.playback.record(payload, received_at)
    results = await deliver(
        state.playback_http,
        [WebhookTarget(id=webhook.id, url=webhook.url) for webhook in webhooks],
        payload,
        state.playback,
        entry=entry,
    )

    message, ok = _test_summary(results)
    return templates.TemplateResponse(
        request,
        "partials/playback_webhooks.html",
        _webhooks_context(
            webhooks, state.playback, test_message=message, test_ok=ok, include_recent=True
        ),
    )


@router.get("/recent", response_class=HTMLResponse)
async def recent_events(request: Request, state: StateDep, admin: AdminPageDep) -> Response:
    """The recent list alone, which the page polls."""
    return templates.TemplateResponse(
        request,
        "partials/playback_recent.html",
        {"recent": list(state.playback.recent)},
    )


def _test_summary(results: list[DeliveryResult]) -> tuple[str, bool]:
    delivered = sum(1 for result in results if result.ok)
    total = len(results)
    if delivered == total:
        noun = "webhook" if total == 1 else f"all {total} webhooks"
        return f"Test event delivered to {noun}.", True
    return (
        f"Test event delivered to {delivered} of {total}. Each webhook's result is"
        " shown beside it.",
        False,
    )


def _validated_url(raw: str) -> str:
    """An http(s) URL with a host, or a 400 that says what is wrong with it.

    Nothing beyond that is checked — in particular not reachability, which the
    test button answers better, and not whether the host is private, because a
    home automation hub on the LAN is the ordinary case.
    """
    url = raw.strip()
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a webhook URL.")
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That URL is longer than {MAX_URL_LENGTH} characters.",
        )
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — raises on a malformed port
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That does not look like a URL."
        ) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A webhook URL has to start with http:// or https:// and name a host.",
        )
    if any(character.isspace() for character in url):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A webhook URL cannot contain spaces."
        )
    return url
