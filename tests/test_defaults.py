"""``GET /defaults`` — the fresh-install seed for Libraries & Home.

Checks the response is exactly what the admin has configured, in
CanopyPlus's own Codable shapes (``MediaLibrary`` fields for
``default_libraries``; ``id``/``title``/``description``/``path``/
``discoverHubKey``/``style``/``titleOnly`` for every shelf-shaped entry) —
and that a caller with no valid Plex token cannot reach any of it.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.session import get_config

from .conftest import SEERR_URL, seerr_user_payload

SHELF_KEYS = {"id", "title", "description", "path", "discoverHubKey", "style", "titleOnly"}
LIBRARY_KEYS = {"id", "serverTitle", "type", "hidden", "name"}


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


async def authenticate(client: httpx.AsyncClient, headers: dict) -> None:
    """Run the tvOS checkpoint so the Plex-token cache is populated."""
    response = await client.get("/register", headers=headers)
    assert response.status_code == 200


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


async def test_a_caller_with_no_plex_token_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/defaults")
    assert response.status_code == 401


async def test_an_unregistered_plex_token_is_rejected(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    # Never authenticated via /register, so nothing is cached for this token.
    response = await client.get("/defaults", headers=plex_headers)
    assert response.status_code == 401


@respx.mock
async def test_the_response_carries_every_field_the_admin_has_configured(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)
    await default_library(db)

    response = await client.get("/defaults", headers=plex_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {
        "default_libraries",
        "default_top_shelf",
        "default_carousel_enabled",
        "default_carousel",
        "default_shelves",
    }

    assert body["default_libraries"] == [
        {
            "id": "1",
            "serverTitle": "Movies (4K HDR)",
            "type": "movie",
            "hidden": False,
            "name": "Movies",
        }
    ]
    assert set(body["default_libraries"][0]) == LIBRARY_KEYS

    assert set(body["default_top_shelf"]) == SHELF_KEYS
    assert set(body["default_carousel"]) == SHELF_KEYS
    assert isinstance(body["default_shelves"], list)
    assert len(body["default_shelves"]) >= 1
    for shelf in body["default_shelves"]:
        assert set(shelf) == SHELF_KEYS

    assert isinstance(body["default_carousel_enabled"], bool)


@respx.mock
async def test_the_response_is_pretty_printed(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    response = await client.get("/defaults", headers=plex_headers)

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
    # these at startup, but the endpoint should never 500 even if that
    # somehow hasn't happened for this row.
    mock_seerr_auth()
    await authenticate(client, plex_headers)
    config = await get_config(db)
    config.home_top_shelf = None
    config.home_carousel = None
    await db.commit()

    response = await client.get("/defaults", headers=plex_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["default_top_shelf"]["path"] == "/library/onDeck"
    assert body["default_carousel"]["path"] == "/library/onDeck"


@respx.mock
async def test_default_carousel_enabled_reflects_the_switch(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)
    config = await get_config(db)
    config.home_carousel_enabled = False
    await db.commit()

    response = await client.get("/defaults", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["default_carousel_enabled"] is False


@respx.mock
async def test_a_custom_shelf_title_is_reflected(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)
    config = await get_config(db)
    # A fresh list, not an in-place mutation of the existing one — assigning
    # back the same list object SQLAlchemy already has on file would not be
    # seen as a change to persist.
    config.home_shelves = [{**config.home_shelves[0], "title": "My Custom Shelf"}]
    await db.commit()

    response = await client.get("/defaults", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["default_shelves"][0]["title"] == "My Custom Shelf"
