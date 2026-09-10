"""``GET``/``PUT /home`` — the per-user Home document, both directions.

The wire shape is CanopyPlus's own ``HomeSettings``, so a good deal of what is
asserted here is compatibility with a decoder that lives in another repository
and cannot be imported: every shelf carries all seven keys including explicit
nulls (``HomeShelfDataModel.init(from:)`` is strict and throws on a missing
one), timestamps are whole-second ``Z`` strings (``.iso8601`` there is
``ISO8601DateFormatter``, which rejects fractional seconds), and a document
that has never been edited omits ``modifiedAt`` rather than inventing one.

The merge itself is the other half: strictly-newer wins, equal is a no-op, and
older loses — with the winning document in the body either way, which is the
whole of the reconciliation protocol.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import User, UserHomeSettings
from cplus_service.db.session import get_config
from cplus_service.home import touched

from .conftest import SEERR_URL, seerr_user_payload

#: Exactly what CanopyPlus's ``.iso8601`` decoding strategy accepts: an
#: internet date-time at second precision, ending in ``Z``.
WIRE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

SHELF_KEYS = {
    "id",
    "title",
    "description",
    "path",
    "discoverHubKey",
    "style",
    "titleOnly",
}


def shelf(**overrides: Any) -> dict[str, Any]:
    """One ``HomeShelfDataModel``-shaped shelf."""
    return {
        "id": "shelf-1",
        "title": "Recently Added",
        "description": "Newest first",
        "path": "/library/sections/1/recentlyAdded",
        "discoverHubKey": None,
        "style": "poster",
        "titleOnly": False,
        **overrides,
    }


def home_payload(**overrides: Any) -> dict[str, Any]:
    """A whole ``HomeSettings`` document, as a client would push one."""
    return {
        "carouselEnabled": True,
        "carouselIncludeOnDeck": False,
        "carouselShelf": shelf(id="carousel-1", title="Featured", style="hero"),
        "homeShelves": [shelf()],
        "topShelf": shelf(id="top-1", title="Up Next", style="card"),
        "modifiedAt": "2026-06-01T12:00:00Z",
        **overrides,
    }


def mock_seerr_auth(**kwargs: Any) -> respx.Route:
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


async def signed_in(client: httpx.AsyncClient, headers: dict) -> None:
    """Prime the token mapping the cache-only routes authenticate against."""
    response = await client.get("/register", headers=headers)
    assert response.status_code == 200


async def only_user(db: AsyncSession) -> User:
    return (await db.execute(select(User))).scalars().one()


async def own_home(db: AsyncSession, user_id: int) -> UserHomeSettings | None:
    return await db.get(UserHomeSettings, user_id)


@pytest.fixture
def stamp() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


async def test_get_home_401s_without_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/home")).status_code == 401


async def test_get_home_401s_for_a_token_that_never_registered(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    # Cache-only auth: no outbound call, so an unknown token is simply a miss.
    assert (await client.get("/home", headers=plex_headers)).status_code == 401


async def test_put_home_401s_for_a_token_that_never_registered(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    response = await client.put("/home", headers=plex_headers, json=home_payload())
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# GET — whose document, and in what shape
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_returns_the_global_default_for_a_user_with_no_home(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.home_shelves = [{**config.home_shelves[0], "title": "Admin's Shelf"}]
    await db.commit()
    await signed_in(client, plex_headers)

    response = await client.get("/home", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["homeShelves"][0]["title"] == "Admin's Shelf"


@respx.mock
async def test_reading_a_home_does_not_fork_the_user_off_the_default(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # Forking on a *read* would silently freeze the user at whatever the global
    # default happened to be the first time anything looked at it.
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)

    await client.get("/home", headers=plex_headers)

    assert await own_home(db, user.id) is None


@respx.mock
async def test_get_prefers_the_users_own_home_once_they_have_one(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)
    db.add(
        UserHomeSettings(
            user_id=user.id,
            home_shelves=[shelf(title="Mine")],
            home_carousel=shelf(title="Mine"),
            home_top_shelf=shelf(title="Mine"),
        )
    )
    await db.commit()

    response = await client.get("/home", headers=plex_headers)

    assert response.json()["homeShelves"][0]["title"] == "Mine"


@respx.mock
async def test_every_shelf_carries_all_seven_keys_including_nulls(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    # CanopyPlus's HomeShelfDataModel decodes strictly: a missing
    # discoverHubKey throws keyNotFound rather than yielding nil.
    mock_seerr_auth()
    await signed_in(client, plex_headers)

    response = await client.get("/home", headers=plex_headers)
    body = response.json()

    shelves = [*body["homeShelves"], body["carouselShelf"], body["topShelf"]]
    for entry in shelves:
        assert set(entry) == SHELF_KEYS
    # Not merely absent-and-therefore-None once parsed: the null has to be on
    # the wire, because the client's decoder distinguishes the two.
    assert any(entry["discoverHubKey"] is None for entry in shelves)
    assert "discoverHubKey" in response.text
    assert "null" in response.text


@respx.mock
async def test_an_unedited_document_omits_modified_at(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    # "Never edited" is absence, which the app's lenient HomeSettings decoder
    # falls back to .distantPast — the one thing that can never win a merge.
    mock_seerr_auth()
    await signed_in(client, plex_headers)

    body = (await client.get("/home", headers=plex_headers)).json()

    assert "modifiedAt" not in body


@respx.mock
async def test_an_edited_document_reports_a_decodable_stamp(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    touched(config)
    await db.commit()
    await signed_in(client, plex_headers)

    body = (await client.get("/home", headers=plex_headers)).json()

    assert WIRE_STAMP.match(body["modifiedAt"]), body["modifiedAt"]


@respx.mock
async def test_a_stamp_written_with_microseconds_still_serialises_cleanly(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # touched() truncates, but nothing stops a migration or a hand-edited row
    # from carrying sub-second precision; it must never reach the client.
    mock_seerr_auth()
    config = await get_config(db)
    config.home_modified_at = datetime(2026, 6, 1, 12, 0, 0, 123456, tzinfo=UTC)
    await db.commit()
    await signed_in(client, plex_headers)

    body = (await client.get("/home", headers=plex_headers)).json()

    assert body["modifiedAt"] == "2026-06-01T12:00:00Z"


# --------------------------------------------------------------------------- #
# PUT — the merge
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_newer_document_wins_and_is_stored(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, stamp: datetime
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.home_modified_at = stamp - timedelta(hours=1)
    await db.commit()
    await signed_in(client, plex_headers)
    newer = (stamp).strftime("%Y-%m-%dT%H:%M:%SZ")

    response = await client.put(
        "/home",
        headers=plex_headers,
        json=home_payload(modifiedAt=newer, homeShelves=[shelf(title="From the TV")]),
    )

    assert response.status_code == 200
    assert response.json()["homeShelves"][0]["title"] == "From the TV"
    assert response.json()["modifiedAt"] == newer

    user = await only_user(db)
    stored = await own_home(db, user.id)
    assert stored is not None
    assert stored.home_shelves[0]["title"] == "From the TV"


@respx.mock
async def test_an_older_document_loses_and_gets_the_winner_back(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, stamp: datetime
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.home_shelves = [shelf(title="Admin wins")]
    config.home_modified_at = stamp
    await db.commit()
    await signed_in(client, plex_headers)
    older = (stamp - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    response = await client.put(
        "/home",
        headers=plex_headers,
        json=home_payload(modifiedAt=older, homeShelves=[shelf(title="Stale")]),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["homeShelves"][0]["title"] == "Admin wins"
    assert body["modifiedAt"] == stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


@respx.mock
async def test_a_loss_writes_nothing(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, stamp: datetime
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.home_modified_at = stamp
    await db.commit()
    await signed_in(client, plex_headers)
    user = await only_user(db)
    older = (stamp - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    await client.put("/home", headers=plex_headers, json=home_payload(modifiedAt=older))

    assert await own_home(db, user.id) is None


@respx.mock
async def test_an_identical_re_push_is_accepted_without_forking(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, stamp: datetime
) -> None:
    # The ordinary case for a client reconnecting. Treating it as a conflict
    # would make an idempotent push look like a failure.
    mock_seerr_auth()
    config = await get_config(db)
    config.home_modified_at = stamp
    await db.commit()
    await signed_in(client, plex_headers)
    user = await only_user(db)
    same = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    response = await client.put(
        "/home", headers=plex_headers, json=home_payload(modifiedAt=same)
    )

    assert response.status_code == 200
    assert await own_home(db, user.id) is None


@respx.mock
async def test_a_first_push_wins_against_a_never_edited_default(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)

    response = await client.put("/home", headers=plex_headers, json=home_payload())

    assert response.status_code == 200
    assert await own_home(db, user.id) is not None


@respx.mock
async def test_an_admin_edit_after_a_client_edit_beats_a_stale_push(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, stamp: datetime
) -> None:
    """The naive/aware comparison this used to trip over, end to end.

    ``touched`` writes an offset-aware stamp; SQLite hands it back naive; the
    client's own stamp arrives offset-aware off the wire. Comparing the two
    without normalising raises ``TypeError`` — a 500 on the merge path.
    """
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)
    db.add(
        UserHomeSettings(
            user_id=user.id,
            home_shelves=[shelf(title="Set by the admin")],
            home_carousel=shelf(),
            home_top_shelf=shelf(),
        )
    )
    await db.commit()
    home = await own_home(db, user.id)
    assert home is not None
    touched(home)
    await db.commit()

    stale = (stamp - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = await client.put(
        "/home", headers=plex_headers, json=home_payload(modifiedAt=stale)
    )

    assert response.status_code == 409
    assert response.json()["homeShelves"][0]["title"] == "Set by the admin"


@respx.mock
async def test_a_push_replicates_the_clients_stamp_rather_than_restamping(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # Re-stamping on arrival would make every push look newer than the edit it
    # carries, and the next device to sync would lose a merge it should win.
    mock_seerr_auth()
    await signed_in(client, plex_headers)

    response = await client.put(
        "/home", headers=plex_headers, json=home_payload(modifiedAt="2026-06-01T12:00:00Z")
    )

    assert response.json()["modifiedAt"] == "2026-06-01T12:00:00Z"


@respx.mock
async def test_a_round_trip_is_byte_for_byte_stable(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    """What a client pushes is exactly what the next ``GET`` hands back.

    The check that would have caught a stored microsecond component being
    dropped on the way out: an echoed document that looked older than what was
    on file would lose its own merge forever.
    """
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    pushed = home_payload(modifiedAt="2026-06-01T12:00:00Z")

    written = await client.put("/home", headers=plex_headers, json=pushed)
    fetched = await client.get("/home", headers=plex_headers)

    assert written.json() == fetched.json() == pushed


# --------------------------------------------------------------------------- #
# PUT — what a client may not send
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (home_payload(homeShelves=[]), "a Home with no shelves at all"),
        (home_payload(modifiedAt=None), "no timestamp to merge on"),
        (home_payload(style="poster"), "an unknown top-level field"),
        (
            home_payload(homeShelves=[{**shelf(), "colour": "green"}]),
            "an unknown field on a shelf the admin UI would then have to render",
        ),
        (
            home_payload(homeShelves=[{**shelf(), "style": "cinemascope"}]),
            "a shelf style CanopyPlus has no case for",
        ),
        (
            home_payload(homeShelves=[{k: v for k, v in shelf().items() if k != "path"}]),
            "a shelf missing a required field",
        ),
    ],
)
async def test_a_malformed_document_is_rejected(
    client: httpx.AsyncClient,
    plex_headers: dict,
    payload: dict,
    why: str,
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)

    response = await client.put("/home", headers=plex_headers, json=payload)

    assert response.status_code == 422, why


@respx.mock
async def test_an_absurd_number_of_shelves_is_rejected(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)

    response = await client.put(
        "/home",
        headers=plex_headers,
        json=home_payload(homeShelves=[shelf(id=f"s{i}") for i in range(101)]),
    )

    assert response.status_code == 422


@respx.mock
async def test_a_rejected_document_writes_nothing(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)

    await client.put("/home", headers=plex_headers, json=home_payload(homeShelves=[]))

    assert await own_home(db, user.id) is None


# --------------------------------------------------------------------------- #
# The first-run bundle carries the same document
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_first_run_bundle_prefers_the_users_own_home(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await signed_in(client, plex_headers)
    user = await only_user(db)
    db.add(
        UserHomeSettings(
            user_id=user.id,
            home_shelves=[shelf(title="Theirs, not the default")],
            home_carousel=shelf(),
            home_top_shelf=shelf(),
        )
    )
    await db.commit()

    response = await client.get(
        "/register", headers=plex_headers, params={"first_run": "true"}
    )

    bundled = response.json()["home"]
    assert bundled["homeShelves"][0]["title"] == "Theirs, not the default"
    assert bundled == (await client.get("/home", headers=plex_headers)).json()
