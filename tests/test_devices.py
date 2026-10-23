"""Connection events from TVs, and the TVs tab that reads them back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.auth.plex_cache import remember_token
from cplus_service.db.models import ClientDevice, ConnectionEvent, User

from .conftest import PLEX_TOKEN
from .test_admin_webui import signed_in

URL = "/devices/connection-events"
LOCAL = "https://192-168-1-5.abc123.plex.direct:32400"
REMOTE = "https://81-2-3-4.abc123.plex.direct:32400"


@pytest.fixture
async def viewer(db: AsyncSession) -> User:
    user = User(seerr_user_id=77, plex_username="viewer")
    db.add(user)
    await db.flush()
    await remember_token(db, PLEX_TOKEN, user)
    await db.commit()
    return user


def headers(device: str = "tv-1", profile: str | None = None) -> dict[str, str]:
    out = {"X-Plex-Token": PLEX_TOKEN, "X-Plex-Client-Identifier": device}
    if profile:
        out["X-Canopy-Profile"] = profile
    return out


def event(
    at: datetime,
    *,
    chosen: tuple[str, str] | None = ("remoteHTTPS", REMOTE),
    network: str = "wifi",
    candidates: list[dict] | None = None,
    **extra,
) -> dict:
    return {
        "id": str(uuid4()),
        "at": at.isoformat(),
        "reason": "launch",
        "network": network,
        "appVersion": "1.5.0",
        "serverMachineID": "machine",
        "previous": None,
        "chosen": {"uri": chosen[1], "tier": chosen[0], "via443Retry": False} if chosen else None,
        "candidates": candidates
        if candidates is not None
        else [
            {"uri": LOCAL, "tier": "localHTTPS", "via443Retry": False,
             "outcome": "dnsFailure", "latencyMs": 12, "httpStatus": None},
            {"uri": REMOTE, "tier": "remoteHTTPS", "via443Retry": False,
             "outcome": "answered", "latencyMs": 88, "httpStatus": None},
        ],
        **extra,
    }


async def device(db: AsyncSession) -> ClientDevice:
    db.expire_all()
    return (await db.execute(select(ClientDevice))).scalar_one()


async def test_needs_the_client_identifier(client: httpx.AsyncClient, viewer: User) -> None:
    response = await client.post(URL, headers={"X-Plex-Token": PLEX_TOKEN}, json={"events": []})
    assert response.status_code == 400


async def test_an_unregistered_token_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, headers=headers(), json={"events": []})
    assert response.status_code == 401


async def test_stores_events_against_the_tv_and_profile(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    viewer_id = viewer.id
    now = datetime.now(UTC)
    response = await client.post(
        URL,
        headers=headers(profile="Kids"),
        json={"deviceName": "Naren’s Apple TV", "events": [event(now)]},
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0}

    tv = await device(db)
    assert tv.user_id == viewer_id
    assert tv.client_identifier == "tv-1"
    assert tv.device_name == "Naren’s Apple TV"
    assert tv.profile == "Kids"
    assert tv.app_version == "1.5.0"
    assert tv.current_tier == "remoteHTTPS"
    assert tv.current_uri == REMOTE
    assert tv.last_lan_at is not None and tv.last_local_at is None

    stored = (await db.execute(select(ConnectionEvent))).scalar_one()
    assert stored.profile == "Kids"
    assert stored.candidates[0]["outcome"] == "dnsFailure"
    assert stored.received_at is not None


async def test_a_resent_event_is_dropped(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    sent = event(datetime.now(UTC))
    await client.post(URL, headers=headers(), json={"events": [sent]})
    response = await client.post(URL, headers=headers(), json={"events": [sent, sent]})
    assert response.json() == {"accepted": 0, "duplicates": 2}
    assert len((await db.execute(select(ConnectionEvent))).scalars().all()) == 1


async def test_the_newest_decision_is_current_whatever_order_it_arrives_in(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    now = datetime.now(UTC)
    await client.post(
        URL, headers=headers(), json={"events": [event(now, chosen=("relay", "https://relay"))]}
    )
    await client.post(
        URL,
        headers=headers(),
        json={"events": [event(now - timedelta(hours=1), chosen=("localHTTPS", LOCAL))]},
    )
    tv = await device(db)
    assert tv.current_tier == "relay"
    # An older local connection still counts: the TV has been local.
    assert tv.last_local_at is not None


async def test_nothing_answered_is_a_current_state(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await client.post(
        URL, headers=headers(), json={"events": [event(datetime.now(UTC), chosen=None)]}
    )
    tv = await device(db)
    assert tv.current_at is not None and tv.current_tier is None


async def test_a_future_clock_is_clamped(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await client.post(
        URL, headers=headers(), json={"events": [event(datetime.now(UTC) + timedelta(days=30))]}
    )
    stored = (await db.execute(select(ConnectionEvent))).scalar_one()
    at = stored.at if stored.at.tzinfo else stored.at.replace(tzinfo=UTC)
    assert at < datetime.now(UTC) + timedelta(minutes=1)


async def test_query_strings_never_reach_the_database(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    leaky = f"{REMOTE}/identity?X-Plex-Token=secret"
    await client.post(
        URL,
        headers=headers(),
        json={"events": [event(datetime.now(UTC), chosen=("remoteHTTPS", leaky), candidates=[
            {"uri": leaky, "tier": "remoteHTTPS", "outcome": "answered"}
        ])]},
    )
    stored = (await db.execute(select(ConnectionEvent))).scalar_one()
    assert "secret" not in stored.chosen_uri
    assert "secret" not in stored.candidates[0]["uri"]


async def test_old_events_are_pruned(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    old = event(datetime.now(UTC))
    await client.post(URL, headers=headers(), json={"events": [old]})
    row = await db.get(ConnectionEvent, old["id"])
    row.received_at = datetime.now(UTC) - timedelta(days=91)
    await db.commit()

    await client.post(URL, headers=headers(), json={"events": [event(datetime.now(UTC))]})
    db.expire_all()
    assert await db.get(ConnectionEvent, old["id"]) is None


async def test_the_same_identifier_on_two_accounts_is_two_tvs(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    other = User(seerr_user_id=78, plex_username="other")
    db.add(other)
    await db.flush()
    await remember_token(db, "other-token", other)
    await db.commit()

    await client.post(URL, headers=headers(), json={"events": [event(datetime.now(UTC))]})
    await client.post(
        URL,
        headers={**headers(), "X-Plex-Token": "other-token"},
        json={"events": [event(datetime.now(UTC))]},
    )
    assert len((await db.execute(select(ClientDevice))).scalars().all()) == 2


async def test_unknown_tiers_and_outcomes_are_kept(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    response = await client.post(
        URL,
        headers=headers(),
        json={"events": [event(datetime.now(UTC), chosen=("peer", "https://x"), candidates=[
            {"uri": "https://x", "tier": "peer", "outcome": "quantum"}
        ])]},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# The TVs tab
# --------------------------------------------------------------------------- #


async def test_the_tab_lists_tvs_with_warnings(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await client.post(
        URL,
        headers=headers(),
        json={"deviceName": "Living room", "events": [
            event(datetime.now(UTC), chosen=("relay", "https://relay.plex.direct:8443"))
        ]},
    )
    await signed_in(client, db)
    page = await client.get("/admin/devices")
    assert page.status_code == 200
    assert "Living room" in page.text
    assert "Relay" in page.text
    assert "Never connected locally" in page.text
    assert "relay, which caps bandwidth" in page.text
    assert "<table" not in page.text


async def test_a_tv_that_has_been_local_has_no_never_local_warning(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await client.post(
        URL, headers=headers(),
        json={"events": [event(datetime.now(UTC), chosen=("localHTTPS", LOCAL))]},
    )
    await signed_in(client, db)
    page = await client.get("/admin/devices")
    assert "Never connected locally" not in page.text


async def test_the_detail_page_shows_the_timeline(
    client: httpx.AsyncClient, db: AsyncSession, viewer: User
) -> None:
    await client.post(
        URL, headers=headers(),
        json={"deviceName": "Den", "events": [event(datetime.now(UTC), chosen=None)]},
    )
    await signed_in(client, db)
    tv = await device(db)
    page = await client.get(f"/admin/devices/{tv.id}")
    assert page.status_code == 200
    assert "no connection" in page.text
    assert "DNS failure" in page.text
    assert "outcome-loud" in page.text
    assert "88 ms" in page.text
    # Grouped best tier first.
    assert page.text.index("Local HTTPS") < page.text.index("Remote HTTPS")


async def test_the_tab_is_gated(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/devices", follow_redirects=False)
    assert response.status_code == 303
