"""``POST /playback/events`` and forwarding to the admin's webhooks.

End to end through the real app, with the webhooks themselves mocked by respx.
``BackgroundTasks`` run before the ASGI transport hands back the response, so a
test can assert on what the webhooks received straight after the call — in
production the same work happens after the Apple TV already has its 202.

The assertions are about the three promises the feature makes: the app is
answered at once and never told about a webhook's trouble; every webhook gets
the documented body; and one bad webhook costs the others nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.auth.plex_cache import remember_token
from cplus_service.db.models import Config, PlaybackWebhook, User
from cplus_service.playback import forward
from cplus_service.playback.events import describe_item
from cplus_service.playback.forward import RECENT_EVENT_LIMIT, redact_url

from .conftest import PLEX_TOKEN

HOOK_A = "http://homeassistant.test:8123/api/webhook/secret-a"
HOOK_B = "http://homeassistant.test:8123/api/webhook/secret-b"
HOOK_C = "https://automations.test/hooks/secret-c"


def movie_event(**overrides: Any) -> dict[str, Any]:
    """A ``started`` event for a movie, exactly as Canopy+ sends one.

    Explicit nulls for the episode fields and the room, not missing keys: that
    is what the app's encoder produces for a movie with no room set.
    """
    body: dict[str, Any] = {
        "event": "started",
        "room": "Living Room",
        "client_id": "ATV-1234",
        "position": 0.0,
        "duration": 8040.2,
        "item": {
            "rating_key": "10488",
            "type": "movie",
            "title": "The Two Towers",
            "show_title": None,
            "season": None,
            "episode": None,
            "year": 2002,
        },
    }
    body.update(overrides)
    return body


def episode_event(**overrides: Any) -> dict[str, Any]:
    body = movie_event(
        item={
            "rating_key": "20001",
            "type": "episode",
            "title": "Pilot",
            "show_title": "Severance",
            "season": 1,
            "episode": 1,
            "year": None,
        }
    )
    body.update(overrides)
    return body


async def viewer(db: AsyncSession) -> User:
    """A tvOS user whose token has been through ``/register``."""
    user = User(seerr_user_id=42, plex_username="robin")
    db.add(user)
    await db.flush()
    await remember_token(db, PLEX_TOKEN, user)
    await db.commit()
    return user


async def switch_on(db: AsyncSession, config: Config) -> None:
    config.playback_events_enabled = True
    await db.commit()


async def add_hooks(db: AsyncSession, *urls: str) -> list[PlaybackWebhook]:
    hooks = [PlaybackWebhook(url=url) for url in urls]
    db.add_all(hooks)
    await db.commit()
    return hooks


def sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


async def test_it_needs_a_plex_token(client: httpx.AsyncClient, configured: Config) -> None:
    response = await client.post("/playback/events", json=movie_event())
    assert response.status_code == 401


async def test_it_refuses_a_token_that_never_registered(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events", json=movie_event(), headers={"X-Plex-Token": "never-seen"}
    )

    assert response.status_code == 401
    assert "/register" in response.json()["detail"]


async def test_it_is_refused_with_a_reason_while_switched_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """403, not 404 or 503: the app treats it as "off" and does not retry."""
    await viewer(db)

    response = await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    assert response.status_code == 403
    assert "switched off" in response.json()["detail"]


async def test_nothing_is_recorded_while_switched_off(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict,
) -> None:
    await viewer(db)

    await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    assert list(app.state.cplus.playback.recent) == []


async def test_it_accepts_an_event_with_202(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    assert response.status_code == 202
    assert response.json() == {"accepted": True}


@pytest.mark.parametrize(
    "event",
    [
        "started", "paused", "resumed", "stopped",
        "credits_started", "intro_started", "intro_ended",
    ],
)
async def test_every_documented_event_is_accepted(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict,
    event: str,
) -> None:
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events", json=movie_event(event=event), headers=plex_headers
    )

    assert response.status_code == 202


async def test_an_unknown_event_is_a_422(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Forwarding an undocumented event would fire webhooks nothing matches."""
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events", json=movie_event(event="scrobbled"), headers=plex_headers
    )

    assert response.status_code == 422


async def test_a_chapter_event_must_name_its_chapter(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events", json=movie_event(event="chapter"), headers=plex_headers
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "broken",
    [
        {"item": None},
        {"position": -1},
        {"duration": "long"},
        {"item": {"rating_key": "1", "type": "clip", "title": "x"}},
    ],
)
async def test_malformed_bodies_are_422(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict,
    broken: dict,
) -> None:
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events", json=movie_event(**broken), headers=plex_headers
    )

    assert response.status_code == 422


async def test_explicit_nulls_are_accepted(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """What Canopy+ actually sends for a movie in a room with no name."""
    await viewer(db)
    await switch_on(db, configured)

    response = await client.post(
        "/playback/events",
        json=movie_event(room=None, watched=None, chapter=None, client_id=None),
        headers=plex_headers,
    )

    assert response.status_code == 202


# --------------------------------------------------------------------------- #
# Forwarding
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_webhook_gets_the_event_plus_who_when_and_where_from(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)
    await add_hooks(db, HOOK_A)
    route = respx.post(HOOK_A).mock(return_value=httpx.Response(200))

    chapter = {"index": 3, "title": "The Battle", "start": 1200.0}
    await client.post(
        "/playback/events",
        json=movie_event(event="chapter", position=1234.5, chapter=chapter),
        headers=plex_headers,
    )

    body = sent(route)
    assert body["event"] == "chapter"
    assert body["room"] == "Living Room"
    assert body["client_id"] == "ATV-1234"
    assert body["position"] == 1234.5
    assert body["duration"] == 8040.2
    assert body["chapter"] == chapter
    assert body["watched"] is None
    assert body["item"] == movie_event()["item"]
    assert body["user"] == {"name": "robin", "profile": ""}
    assert body["source"] == "canopy-plus"
    assert body["test"] is False
    assert body["received_at"].endswith("Z")
    assert set(body) == {
        "event", "room", "client_id", "position", "duration", "watched", "chapter",
        "item", "user", "received_at", "source", "test",
    }


@respx.mock
async def test_the_profile_header_is_passed_along_when_sent(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)
    await add_hooks(db, HOOK_A)
    route = respx.post(HOOK_A).mock(return_value=httpx.Response(204))

    await client.post(
        "/playback/events",
        json=movie_event(),
        headers={**plex_headers, "X-Canopy-Profile": "kids"},
    )

    assert sent(route)["user"] == {"name": "robin", "profile": "kids"}


@respx.mock
async def test_watched_is_forwarded_on_stopped_and_cleared_elsewhere(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)
    await add_hooks(db, HOOK_A)
    route = respx.post(HOOK_A).mock(return_value=httpx.Response(200))

    await client.post(
        "/playback/events", json=movie_event(event="stopped", watched=True), headers=plex_headers
    )
    assert sent(route)["watched"] is True

    await client.post(
        "/playback/events", json=movie_event(event="paused", watched=True), headers=plex_headers
    )
    assert sent(route)["watched"] is None


@respx.mock
async def test_every_webhook_gets_every_event(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await viewer(db)
    await switch_on(db, configured)
    await add_hooks(db, HOOK_A, HOOK_B)
    a = respx.post(HOOK_A).mock(return_value=httpx.Response(200))
    b = respx.post(HOOK_B).mock(return_value=httpx.Response(200))

    await client.post("/playback/events", json=episode_event(), headers=plex_headers)

    assert a.call_count == 1
    assert b.call_count == 1
    assert sent(a) == sent(b)


@respx.mock
async def test_failing_webhooks_do_not_stop_the_others_or_reach_the_app(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict,
) -> None:
    await viewer(db)
    await switch_on(db, configured)
    a, b, c = await add_hooks(db, HOOK_A, HOOK_B, HOOK_C)
    respx.post(HOOK_A).mock(side_effect=httpx.ConnectError("Connection refused"))
    respx.post(HOOK_B).mock(return_value=httpx.Response(500))
    good = respx.post(HOOK_C).mock(return_value=httpx.Response(200))

    response = await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    assert response.status_code == 202
    assert good.call_count == 1
    results = app.state.cplus.playback.last_results
    assert results[a.id].ok is False and "Could not connect" in results[a.id].summary
    assert results[b.id].ok is False and results[b.id].status_code == 500
    assert results[c.id].ok is True and results[c.id].status_code == 200
    entry = app.state.cplus.playback.recent[0]
    assert entry.delivered == 1 and len(entry.deliveries) == 3


@respx.mock
async def test_an_unexpected_exception_is_contained(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict,
) -> None:
    """Delivery runs where nobody can catch anything, so nothing may escape it."""
    await viewer(db)
    await switch_on(db, configured)
    (hook,) = await add_hooks(db, HOOK_A)
    respx.post(HOOK_A).mock(side_effect=RuntimeError("boom"))

    response = await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    assert response.status_code == 202
    assert app.state.cplus.playback.last_results[hook.id].error == "RuntimeError: boom"


@respx.mock
async def test_a_slow_webhook_times_out_without_delaying_the_others(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forward, "DELIVERY_TIMEOUT_SECONDS", 0.2)
    await viewer(db)
    await switch_on(db, configured)
    slow, quick = await add_hooks(db, HOOK_A, HOOK_B)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    respx.post(HOOK_A).mock(side_effect=hang)
    respx.post(HOOK_B).mock(return_value=httpx.Response(200))

    loop = asyncio.get_running_loop()
    started = loop.time()
    await client.post("/playback/events", json=movie_event(), headers=plex_headers)
    elapsed = loop.time() - started

    results = app.state.cplus.playback.last_results
    assert results[quick.id].ok is True
    assert results[slow.id].ok is False
    assert "Timed out" in results[slow.id].summary
    assert elapsed < 2


async def test_events_are_recorded_even_with_nowhere_to_send_them(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict,
) -> None:
    """How an admin watches the app's events arrive before writing an automation."""
    await viewer(db)
    await switch_on(db, configured)

    await client.post("/playback/events", json=episode_event(event="paused"), headers=plex_headers)

    (entry,) = app.state.cplus.playback.recent
    assert entry.event == "paused"
    assert entry.room == "Living Room"
    assert entry.user == "robin"
    assert entry.title == "Severance · S1E1 · Pilot"
    assert entry.deliveries == []


async def test_the_recent_list_keeps_only_the_newest(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, configured: Config,
    plex_headers: dict,
) -> None:
    await viewer(db)
    await switch_on(db, configured)

    for second in range(RECENT_EVENT_LIMIT + 5):
        await client.post(
            "/playback/events", json=movie_event(position=float(second)), headers=plex_headers
        )

    recent = list(app.state.cplus.playback.recent)
    assert len(recent) == RECENT_EVENT_LIMIT
    assert recent[0].payload["position"] == float(RECENT_EVENT_LIMIT + 4)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_a_logged_url_keeps_its_host_and_loses_its_secret() -> None:
    assert redact_url(HOOK_A) == "http://homeassistant.test:8123/…"
    assert redact_url("https://[::1]/hook/x") == "https://[::1]/…"
    assert "secret" not in redact_url(HOOK_C)
    assert redact_url("not a url") == "(unparseable URL)"


def test_items_are_named_the_way_the_recent_list_shows_them() -> None:
    assert describe_item(movie_event()["item"]) == "The Two Towers (2002)"
    assert describe_item({"type": "movie", "title": "Untimed", "year": None}) == "Untimed"
    assert describe_item(episode_event()["item"]) == "Severance · S1E1 · Pilot"
