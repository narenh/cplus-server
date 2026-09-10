"""``GET /register`` — the always-on ``plex_server`` identity and the
``first_run`` bundle.

There used to be a separate ``GET /defaults`` endpoint; it was folded into
``GET /register`` entirely (see ``register.register``'s ``first_run``
parameter) rather than kept alongside it, so an actual first run needs no
second round trip. ``defaults_payload`` is what still builds the bundle.

Checks the bundle is exactly what the admin has configured, in CanopyPlus's
own Codable shapes with no extra fields (``MediaLibrary`` fields for
``default_libraries``; a whole ``HomeSettings`` document for ``home``). The
Home document's own shape, and both directions of its sync, are covered in
``test_home_sync.py``; what matters here is that the bundle carries the same
one. Auth rejection for ``/register`` itself (no token, a bad token) is
covered in ``test_api_auth.py``, not repeated here.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.session import get_config

from .conftest import SEERR_URL, seerr_user_payload

SHELF_KEYS = {
    "id",
    "title",
    "description",
    "path",
    "discoverHubKey",
    "style",
    "titleOnly",
}
LIBRARY_KEYS = {"id", "serverTitle", "type", "hidden", "name"}
HOME_KEYS = {
    "carouselEnabled",
    "carouselIncludeOnDeck",
    "carouselShelf",
    "homeShelves",
    "topShelf",
}


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


async def first_run(client: httpx.AsyncClient, headers: dict) -> httpx.Response:
    return await client.get("/register", headers=headers, params={"first_run": "true"})


async def default_library(db: AsyncSession, *, library_id: str = "1") -> None:
    config = await get_config(db)
    config.default_libraries = [
        {
            "id": library_id,
            "serverTitle": "Movies (4K HDR)",
            "type": "movie",
            "hidden": False,
            "name": "Movies",
        }
    ]
    await db.commit()


def assert_well_formed_payload(body: dict) -> None:
    assert set(body) == {"default_libraries", "home"}
    for library in body["default_libraries"]:
        assert set(library) == LIBRARY_KEYS

    home = body["home"]
    # ``modifiedAt`` is the one optional key — absent means "never edited".
    assert set(home) - {"modifiedAt"} == HOME_KEYS
    assert isinstance(home["carouselEnabled"], bool)
    assert isinstance(home["carouselIncludeOnDeck"], bool)
    assert isinstance(home["homeShelves"], list)
    assert len(home["homeShelves"]) >= 1
    for shelf in [*home["homeShelves"], home["carouselShelf"], home["topShelf"]]:
        assert set(shelf) == SHELF_KEYS


# --------------------------------------------------------------------------- #
# plex_server — on every call, not just the first run
# --------------------------------------------------------------------------- #


@respx.mock
async def test_plex_server_is_null_when_no_server_is_connected(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "plex_server": None}


@respx.mock
async def test_plex_server_reports_the_connected_server(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.plex_server_client_identifier = "abc123machine"
    config.plex_server_name = "Tower"
    await db.commit()

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["plex_server"] == {
        "client_identifier": "abc123machine",
        "name": "Tower",
    }


@respx.mock
async def test_plex_server_never_leaks_the_base_url(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # The address *this service* reaches Plex on is frequently not one the
    # client could use, and is none of its business either way.
    mock_seerr_auth()
    config = await get_config(db)
    config.plex_server_client_identifier = "abc123machine"
    config.plex_server_base_url = "http://192.168.1.50:32400"
    await db.commit()

    response = await first_run(client, plex_headers)

    assert "192.168.1.50" not in response.text


# --------------------------------------------------------------------------- #
# GET /register's first_run bundling
# --------------------------------------------------------------------------- #


@respx.mock
async def test_register_omits_defaults_when_first_run_is_absent(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 200
    assert set(response.json()) == {"status", "plex_server"}


@respx.mock
async def test_register_omits_defaults_when_first_run_is_false(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()

    response = await client.get(
        "/register", headers=plex_headers, params={"first_run": "false"}
    )

    assert response.status_code == 200
    assert set(response.json()) == {"status", "plex_server"}


@respx.mock
async def test_register_bundles_defaults_when_first_run_is_true(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await default_library(db)

    response = await first_run(client, plex_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["status"] == "ok"
    rest = {k: v for k, v in body.items() if k not in {"status", "plex_server"}}
    assert_well_formed_payload(rest)
    assert rest["default_libraries"] == [
        {
            "id": "1",
            "serverTitle": "Movies (4K HDR)",
            "type": "movie",
            "hidden": False,
            "name": "Movies",
        }
    ]


@respx.mock
async def test_default_libraries_are_not_capped(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # The client filters this down to what the signed-in user can actually see
    # and takes the first few; an admin naming more than any one user can reach
    # is the ordinary case, so nothing is trimmed here.
    mock_seerr_auth()
    config = await get_config(db)
    config.default_libraries = [
        {
            "id": str(index),
            "serverTitle": f"Library {index}",
            "type": "movie",
            "hidden": False,
            "name": f"Library {index}",
        }
        for index in range(12)
    ]
    await db.commit()

    response = await first_run(client, plex_headers)

    assert len(response.json()["default_libraries"]) == 12


@respx.mock
async def test_the_bundled_response_is_pretty_printed(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()

    response = await first_run(client, plex_headers)

    assert response.status_code == 200
    # Prettified for now while this is still being built out and read by
    # eye — a later pass switches this to compact JSON.
    assert response.text == json.dumps(response.json(), indent=2)
    assert "\n" in response.text


@respx.mock
async def test_top_shelf_and_carousel_fall_back_when_unset(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # Belt-and-suspenders: ensure_default_carousel/_top_shelf already seed
    # these at startup, but the bundle should never 500 even if that
    # somehow hasn't happened for this row.
    mock_seerr_auth()
    config = await get_config(db)
    config.home_top_shelf = None
    config.home_carousel = None
    await db.commit()

    response = await first_run(client, plex_headers)

    assert response.status_code == 200
    home = response.json()["home"]
    assert home["topShelf"]["path"] == "/library/onDeck"
    assert home["carouselShelf"]["path"] == "/library/onDeck"


@respx.mock
async def test_default_carousel_enabled_reflects_the_switch(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    config.home_carousel_enabled = False
    await db.commit()

    response = await first_run(client, plex_headers)

    assert response.status_code == 200
    assert response.json()["home"]["carouselEnabled"] is False


@respx.mock
async def test_a_custom_shelf_title_is_reflected(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    config = await get_config(db)
    # A fresh list, not an in-place mutation of the existing one — assigning
    # back the same list object SQLAlchemy already has on file would not be
    # seen as a change to persist.
    config.home_shelves = [{**config.home_shelves[0], "title": "My Custom Shelf"}]
    await db.commit()

    response = await first_run(client, plex_headers)

    assert response.status_code == 200
    assert response.json()["home"]["homeShelves"][0]["title"] == "My Custom Shelf"


@respx.mock
async def test_register_still_401s_on_a_bad_token_regardless_of_first_run(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(return_value=httpx.Response(403))

    response = await first_run(client, plex_headers)

    assert response.status_code == 401
