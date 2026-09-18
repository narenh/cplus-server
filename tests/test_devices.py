"""The Plex device identifier: recording it, and naming the device it names.

Two halves, and they are independent of each other. The recording half is an
optional pair of headers on the ordinary client endpoints — nothing about a
grab or a request may change because a client did or did not send them. The
naming half is an admin page over the registry those sightings build up.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ActivityLog, Config, Grab, PlexDevice, User

from .conftest import PROWLARR_URL, SEERR_URL, grant, make_action, seerr_user_payload
from .test_admin_webui import signed_in

GB = 1024**3

LIVING_ROOM = "5ad1f1de-0c8e-4e2b-9a55-1f7f1c3d9e11"
BEDROOM = "c0ffee00-1111-2222-3333-444455556666"

RELEASE = {
    "action_id": None,  # filled in per test
    "release_guid": "guid-uhd",
    "indexer_id": 1,
    "release_title": "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
    "size_bytes": 25 * GB,
}


def device_headers(identifier: str, name: str | None = None) -> dict[str, str]:
    """What a client sends alongside its Plex token."""
    headers = {"X-Plex-Client-Identifier": identifier}
    if name is not None:
        headers["X-Plex-Device-Name"] = name
    return headers


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


def mock_prowlarr_grab() -> respx.Route:
    return respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )


async def a_permitted_action(client: httpx.AsyncClient, db: AsyncSession, headers: dict):  # noqa: ANN201
    """A registered caller holding one ordinary action."""
    assert (await client.get("/register", headers=headers)).status_code == 200
    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)
    return user, action


# --------------------------------------------------------------------------- #
# Recording the identifier
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_grab_records_the_device_that_made_it(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_grab()
    headers = {**plex_headers, **device_headers(LIVING_ROOM, "Living Room Apple TV")}
    user, action = await a_permitted_action(client, db, headers)

    response = await client.post(
        "/grab", json={**RELEASE, "action_id": action.id}, headers=headers
    )
    assert response.status_code == 200

    grab = (await db.execute(select(Grab))).scalar_one()
    assert grab.device_identifier == LIVING_ROOM

    # Both tables: the grabs page reads one and the activity log the other, and
    # neither joins to the other to find out where a row came from.
    events = (await db.execute(select(ActivityLog))).scalars().all()
    assert {event.device_identifier for event in events} == {LIVING_ROOM}


@respx.mock
async def test_a_first_sighting_registers_the_device_with_the_name_it_reports(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """No enrollment step: the registry is a by-product of ordinary traffic."""
    mock_seerr_auth()
    headers = {**plex_headers, **device_headers(LIVING_ROOM, "Living Room Apple TV")}
    await a_permitted_action(client, db, headers)

    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    assert device.device_name == "Living Room Apple TV"
    # Not named by anyone yet, so its label is what it calls itself.
    assert device.nickname is None
    assert device.label == "Living Room Apple TV"


@respx.mock
async def test_a_client_that_sends_nothing_is_served_and_recorded_without_a_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """The headers are additive. Every client predating them keeps working."""
    mock_seerr_auth()
    mock_prowlarr_grab()
    user, action = await a_permitted_action(client, db, plex_headers)

    response = await client.post(
        "/grab", json={**RELEASE, "action_id": action.id}, headers=plex_headers
    )

    assert response.status_code == 200
    grab = (await db.execute(select(Grab))).scalar_one()
    assert grab.device_identifier is None
    assert (await db.execute(select(PlexDevice))).scalars().all() == []


@respx.mock
@pytest.mark.parametrize(
    "identifier",
    [
        pytest.param("   ", id="blank"),
        pytest.param("x" * 129, id="longer than the column"),
        pytest.param("\n\t", id="control characters only"),
    ],
)
async def test_an_unusable_identifier_never_costs_the_caller_their_grab(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    identifier: str,
) -> None:
    """A malformed header is a client bug, not a reason to refuse a user.

    It is dropped rather than truncated: a truncated identifier is a *different*
    device to every lookup, so two clients could silently become one row.
    """
    mock_seerr_auth()
    mock_prowlarr_grab()
    headers = {**plex_headers, **device_headers(identifier)}
    user, action = await a_permitted_action(client, db, headers)

    response = await client.post(
        "/grab", json={**RELEASE, "action_id": action.id}, headers=headers
    )

    assert response.status_code == 200
    grab = (await db.execute(select(Grab))).scalar_one()
    assert grab.device_identifier is None
    assert (await db.execute(select(PlexDevice))).scalars().all() == []


@respx.mock
async def test_a_returning_device_refreshes_its_name_and_keeps_its_nickname(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """An admin's label outlives whatever the client renames itself to."""
    mock_seerr_auth()
    headers = {**plex_headers, **device_headers(LIVING_ROOM, "Apple TV")}
    await a_permitted_action(client, db, headers)

    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    device.nickname = "Living Room"
    await db.commit()

    renamed = {**plex_headers, **device_headers(LIVING_ROOM, "Apple TV 4K")}
    assert (await client.get("/register", headers=renamed)).status_code == 200

    await db.refresh(device)
    assert device.device_name == "Apple TV 4K"
    assert device.nickname == "Living Room"
    assert device.label == "Living Room"
    assert (await db.execute(select(PlexDevice))).scalars().all() == [device]


@respx.mock
async def test_a_client_that_stops_sending_a_name_keeps_the_last_one(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Otherwise the row falls back to a bare UUID in the console."""
    mock_seerr_auth()
    named = {**plex_headers, **device_headers(LIVING_ROOM, "Living Room Apple TV")}
    await a_permitted_action(client, db, named)

    bare = {**plex_headers, **device_headers(LIVING_ROOM)}
    assert (await client.get("/register", headers=bare)).status_code == 200

    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    await db.refresh(device)
    assert device.device_name == "Living Room Apple TV"


@respx.mock
async def test_a_request_records_its_device_too(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Not just grabs: every event a device causes says which device it was."""
    mock_seerr_auth()
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 7})
    )
    headers = {**plex_headers, **device_headers(BEDROOM, "Bedroom iPad")}
    assert (await client.get("/register", headers=headers)).status_code == 200

    user = (await db.execute(select(User))).scalar_one()
    from cplus_service.bootstrap import get_request_action

    action = await get_request_action(db)
    assert action is not None
    await grant(db, user, action)

    response = await client.post(
        "/request", json={"tmdb_id": 550, "type": "movie"}, headers=headers
    )
    assert response.status_code == 200

    events = (await db.execute(select(ActivityLog))).scalars().all()
    assert [event.device_identifier for event in events if event.detail.get("kind")] == [
        BEDROOM
    ]


# --------------------------------------------------------------------------- #
# The Devices page
# --------------------------------------------------------------------------- #


async def a_known_device(
    db: AsyncSession,
    *,
    identifier: str = LIVING_ROOM,
    device_name: str | None = "Living Room Apple TV",
    nickname: str | None = None,
) -> PlexDevice:
    device = PlexDevice(
        client_identifier=identifier, device_name=device_name, nickname=nickname
    )
    db.add(device)
    await db.commit()
    return device


async def test_the_devices_page_needs_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/devices", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@respx.mock
async def test_the_devices_page_lists_what_has_been_seen(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await a_known_device(db)

    page = await client.get("/admin/devices")

    assert page.status_code == 200
    assert "Living Room Apple TV" in page.text
    # The whole identifier, not the truncated form the tables print: this is the
    # page where an admin matches one against a client's own settings screen.
    assert LIVING_ROOM in page.text


@respx.mock
async def test_the_devices_page_says_so_when_nothing_has_been_seen(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)

    page = await client.get("/admin/devices")

    assert page.status_code == 200
    assert "No devices seen yet" in page.text


@respx.mock
async def test_naming_a_device_shows_that_name_on_the_grabs_page(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """The whole point of the nickname: the tables stop printing a UUID."""
    admin = await signed_in(client, db)
    await a_known_device(db)
    action = await make_action(db, "Stream Now")
    db.add(
        Grab(
            user_id=admin.id,
            action_id=action.id,
            device_identifier=LIVING_ROOM,
            release_title="Movie.2024.2160p.WEB-DL-FLUX",
            release_guid="g1",
            indexer_id=1,
            size_bytes=25 * GB,
        )
    )
    await db.commit()

    saved = await client.post(
        "/admin/devices/nickname",
        data={"client_identifier": LIVING_ROOM, "nickname": "  Living   Room  "},
    )
    assert saved.status_code == 200
    # Collapsed, not stored as typed.
    assert "Living Room" in saved.text

    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    await db.refresh(device)
    assert device.nickname == "Living Room"

    grabs = await client.get("/admin/grabs")
    assert "Living Room" in grabs.text
    activity = await client.get("/admin/activity-log")
    assert activity.status_code == 200


@respx.mock
async def test_clearing_the_nickname_falls_back_to_what_the_client_calls_itself(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await a_known_device(db, nickname="Living Room")

    saved = await client.post(
        "/admin/devices/nickname",
        data={"client_identifier": LIVING_ROOM, "nickname": ""},
    )

    assert saved.status_code == 200
    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    await db.refresh(device)
    assert device.nickname is None
    assert device.label == "Living Room Apple TV"


@respx.mock
async def test_an_overlong_nickname_is_refused_rather_than_truncated(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await a_known_device(db)

    refused = await client.post(
        "/admin/devices/nickname",
        data={"client_identifier": LIVING_ROOM, "nickname": "x" * 65},
    )

    assert refused.status_code == 400
    device = await db.get(PlexDevice, LIVING_ROOM)
    assert device is not None
    await db.refresh(device)
    assert device.nickname is None


@respx.mock
async def test_naming_a_device_that_is_gone_says_so(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """A stale form from another tab, not a reason to resurrect a row."""
    await signed_in(client, db)

    refused = await client.post(
        "/admin/devices/nickname",
        data={"client_identifier": LIVING_ROOM, "nickname": "Living Room"},
    )

    assert refused.status_code == 404
    assert (await db.execute(select(PlexDevice))).scalars().all() == []


@respx.mock
async def test_removing_a_device_keeps_the_grabs_it_made(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """There is deliberately no foreign key: a label goes, history does not."""
    admin = await signed_in(client, db)
    await a_known_device(db, nickname="Living Room")
    action = await make_action(db, "Stream Now")
    db.add(
        Grab(
            user_id=admin.id,
            action_id=action.id,
            device_identifier=LIVING_ROOM,
            release_title="Movie.2024.2160p.WEB-DL-FLUX",
            release_guid="g1",
            indexer_id=1,
            size_bytes=25 * GB,
        )
    )
    await db.commit()

    removed = await client.post(
        "/admin/devices/delete", data={"client_identifier": LIVING_ROOM}
    )

    assert removed.status_code == 200
    assert "No devices seen yet" in removed.text
    assert (await db.execute(select(PlexDevice))).scalars().all() == []

    grab = (await db.execute(select(Grab))).scalar_one()
    assert grab.device_identifier == LIVING_ROOM

    # The identifier is still printed, truncated, with no name to print instead.
    page = await client.get("/admin/grabs")
    assert "Living Room" not in page.text
    assert LIVING_ROOM[:8] in page.text
