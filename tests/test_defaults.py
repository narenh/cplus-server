"""``GET /register``'s ``first_run`` bundling.

There used to be a separate ``GET /defaults`` endpoint; it was folded into
``GET /register`` entirely (see ``register.register``'s ``first_run``
parameter) rather than kept alongside it, so an actual first run needs no
second round trip. ``defaults_payload`` is what still builds the bundle.

Checks the bundle is exactly what the admin has configured, in CanopyPlus's
own Codable shapes with no extra fields (``MediaLibrary`` fields for
``default_libraries``; ``id``/``title``/``description``/``path``/
``discoverHubKey``/``style``/``titleOnly`` for every shelf-shaped entry).
Auth rejection for ``/register`` itself (no token, a bad token) is covered
in ``test_api_auth.py``, not repeated here.
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
CAROUSEL_KEYS = {"enabled", "include_on_deck", "carousel", "top_shelf"}


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
    assert set(body) == {"default_libraries", "default_home_shelves", "default_carousel"}
    for library in body["default_libraries"]:
        assert set(library) == LIBRARY_KEYS

    assert isinstance(body["default_home_shelves"], list)
    assert len(body["default_home_shelves"]) >= 1
    for shelf in body["default_home_shelves"]:
        assert set(shelf) == SHELF_KEYS

    carousel = body["default_carousel"]
    assert set(carousel) == CAROUSEL_KEYS
    assert isinstance(carousel["enabled"], bool)
    assert isinstance(carousel["include_on_deck"], bool)
    assert set(carousel["carousel"]) == SHELF_KEYS
    assert set(carousel["top_shelf"]) == SHELF_KEYS


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
    assert response.json() == {"status": "ok"}


@respx.mock
async def test_register_omits_defaults_when_first_run_is_false(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()

    response = await client.get(
        "/register", headers=plex_headers, params={"first_run": "false"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    rest = {k: v for k, v in body.items() if k != "status"}
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
    carousel = response.json()["default_carousel"]
    assert carousel["top_shelf"]["path"] == "/library/onDeck"
    assert carousel["carousel"]["path"] == "/library/onDeck"


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
    assert response.json()["default_carousel"]["enabled"] is False


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
    assert response.json()["default_home_shelves"][0]["title"] == "My Custom Shelf"


@respx.mock
async def test_register_still_401s_on_a_bad_token_regardless_of_first_run(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(return_value=httpx.Response(403))

    response = await first_run(client, plex_headers)

    assert response.status_code == 401
