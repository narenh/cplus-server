"""The Playback tab.

Drives the real templates, so a page that will not render shows up here rather
than in a browser. About what an admin can see and do: the switch persists and
reaches ``/capabilities``, webhooks can be added and removed and bad ones are
refused with a reason, the test button reports each webhook's own result, and
events the app sent show up in the recent list.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Config, PlaybackWebhook
from cplus_service.playback.events import PlaybackEventKind

from .test_admin_webui import signed_in
from .test_playback import HOOK_A, HOOK_B, add_hooks, movie_event, switch_on, viewer


async def hook_urls(db: AsyncSession) -> list[str]:
    rows = await db.execute(select(PlaybackWebhook.url).order_by(PlaybackWebhook.id))
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


async def test_the_tab_is_gated_like_every_other_admin_page(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/admin/playback", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_the_page_renders_off_with_its_documentation(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.get("/admin/playback")

    assert response.status_code == 200
    body = response.text
    assert 'href="/admin/playback" class="on"' in body
    assert 'name="enabled"' in body and "checked" not in body
    assert "No webhooks yet" in body
    assert "Nothing received since the server started" in body
    # The documented payload and a real example of it.
    assert "&#34;source&#34;: &#34;canopy-plus&#34;" in body or '"source": "canopy-plus"' in body
    assert "trigger.json" in body


async def test_every_event_is_described_on_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """A new event added to the schema must not go undocumented here."""
    await signed_in(client, db)

    body = (await client.get("/admin/playback")).text

    for kind in PlaybackEventKind:
        assert f"<code>{kind.value}</code>" in body


async def test_the_page_uses_no_table(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """CLAUDE.md: new UI lays lists out as lists, so they reflow on a phone."""
    await signed_in(client, db)
    await add_hooks(db, HOOK_A)
    await viewer(db)
    await switch_on(db, configured)
    await client.post("/playback/events", json=movie_event(), headers=plex_headers)

    body = (await client.get("/admin/playback")).text

    assert "<table" not in body


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


async def test_the_switch_turns_on_and_off_and_reaches_capabilities(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    on = await client.post("/admin/playback/enabled", data={"enabled": "on"})
    assert on.status_code == 200
    assert "checked" in on.text
    assert (await client.get("/capabilities")).json()["playback_events"] is True

    off = await client.post("/admin/playback/enabled", data={})
    assert "checked" not in off.text
    assert (await client.get("/capabilities")).json()["playback_events"] is False


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #


async def test_a_webhook_can_be_added(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/playback/webhooks", data={"url": f"  {HOOK_A} "})

    assert response.status_code == 200
    assert HOOK_A in response.text
    assert "Nothing sent yet" in response.text
    assert await hook_urls(db) == [HOOK_A]


async def test_bad_urls_are_refused_with_a_reason(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    for url in ["", "homeassistant.local/hook", "ftp://example.test/x", "http://", "http://a b/"]:
        response = await client.post("/admin/playback/webhooks", data={"url": url})
        assert response.status_code == 400, url
        assert response.json()["detail"]

    assert await hook_urls(db) == []


async def test_the_same_url_cannot_be_added_twice(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Two rows for one URL would fire the same automation twice per event."""
    await signed_in(client, db)
    await add_hooks(db, HOOK_A)

    response = await client.post("/admin/playback/webhooks", data={"url": HOOK_A})

    assert response.status_code == 409
    assert await hook_urls(db) == [HOOK_A]


async def test_a_webhook_can_be_removed(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    a, _ = await add_hooks(db, HOOK_A, HOOK_B)

    response = await client.post(f"/admin/playback/webhooks/{a.id}/delete")

    assert response.status_code == 200
    assert HOOK_A not in response.text
    assert HOOK_B in response.text
    assert await hook_urls(db) == [HOOK_B]


async def test_removing_a_webhook_that_is_already_gone_is_harmless(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/playback/webhooks/999/delete")

    assert response.status_code == 200
    assert "No webhooks yet" in response.text


# --------------------------------------------------------------------------- #
# The test button
# --------------------------------------------------------------------------- #


async def test_the_test_button_asks_for_a_webhook_first(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/playback/test")

    assert response.status_code == 200
    assert "Add a webhook URL first" in response.text


@respx.mock
async def test_the_test_button_sends_a_chapter_event_and_shows_each_result(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Works with the switch off: setting up the receiving end comes first."""
    await signed_in(client, db)
    await add_hooks(db, HOOK_A, HOOK_B)
    good = respx.post(HOOK_A).mock(return_value=httpx.Response(200))
    respx.post(HOOK_B).mock(return_value=httpx.Response(404))

    response = await client.post("/admin/playback/test")

    assert response.status_code == 200
    body = json.loads(good.calls.last.request.content)
    assert body["event"] == "chapter"
    assert body["chapter"]["title"] == "The Battle"
    assert body["test"] is True
    assert body["user"] == {"name": "owner", "profile": ""}
    assert body["source"] == "canopy-plus"

    assert "Delivered (HTTP 200)" in response.text
    assert "Failed: HTTP 404" in response.text
    assert "delivered to 1 of 2" in response.text
    # The recent list rides along out of band, carrying the test event.
    assert 'hx-swap-oob="true"' in response.text
    assert "The End of Oak Street" in response.text


@respx.mock
async def test_results_survive_a_reload_of_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await add_hooks(db, HOOK_A)
    respx.post(HOOK_A).mock(side_effect=httpx.ConnectError("Connection refused"))

    await client.post("/admin/playback/test")
    body = (await client.get("/admin/playback")).text

    assert "Failed: Could not connect" in body


# --------------------------------------------------------------------------- #
# Recent events
# --------------------------------------------------------------------------- #


async def test_events_from_the_app_show_in_the_recent_list(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    await signed_in(client, db)
    await viewer(db)
    await switch_on(db, configured)
    await client.post(
        "/playback/events",
        json=movie_event(event="chapter", chapter={"index": 0, "title": "Prologue"}),
        headers=plex_headers,
    )
    await client.post(
        "/playback/events",
        json=movie_event(event="stopped", watched=True, room=None),
        headers=plex_headers,
    )

    body = (await client.get("/admin/playback/recent")).text

    assert "The Two Towers (2002)" in body
    assert "Prologue" in body
    assert "watched" in body
    assert "Living Room · robin" in body
    assert "No room set · robin" in body
    # Newest first.
    assert body.index("stopped") < body.index("chapter")


async def test_the_recent_list_is_gated(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/playback/recent", follow_redirects=False)
    assert response.status_code == 303
