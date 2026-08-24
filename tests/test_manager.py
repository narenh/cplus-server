"""``POST /manager/grab`` and ``GET /manager/download-clients``.

The admin app's request-manager endpoints: live Seerr auth, gated on
``MANAGE_REQUESTS``, no action involved. Split out from test_api_endpoints.py
because these calls used to live at ``POST /grab`` and ``GET /download-clients``
— naming a download client directly was one of two branches on the tvOS grab
endpoint — and now live under their own ``/manager`` prefix instead.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Config, Grab

from .conftest import PROWLARR_URL, SEERR_URL, seerr_user_payload

GB = 1024**3

WEB_2160 = {
    "title": "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
    "guid": "guid-uhd",
    "indexerId": 1,
    "indexer": "Tracker One",
    "size": 25 * GB,
}


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


def seerr_rejects_the_token() -> respx.Route:
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorised"})
    )


def seerr_is_unreachable() -> respx.Route:
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("connection refused")
    )


DIRECT_GRAB_BODY = {
    "download_client_id": 9,
    "release_guid": "guid-uhd",
    "indexer_id": 1,
    "release_title": WEB_2160["title"],
}


# --------------------------------------------------------------------------- #
# POST /manager/grab
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_request_manager_can_grab_without_an_action(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Actions are a tvOS concept — a button label and a recommendation. An admin
    # picking a specific release during an approval names the client directly.
    mock_seerr_auth(permissions=2)
    grab_route = respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )

    response = await client.post(
        "/manager/grab",
        json={
            "download_client_id": 9,
            "release_guid": "guid-uhd",
            "indexer_id": 1,
            "release_title": WEB_2160["title"],
            "size_bytes": 25 * GB,
        },
        headers=plex_headers,
    )

    assert response.status_code == 200
    assert json.loads(grab_route.calls[0].request.content)["downloadClientId"] == 9

    record = (await db.execute(select(Grab))).scalar_one()
    assert record.action_id is None  # no action was involved
    assert record.release_title == WEB_2160["title"]


@respx.mock
async def test_a_regular_user_cannot_grab_without_an_action(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=32)
    grab_route = respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )

    response = await client.post(
        "/manager/grab",
        json={
            "download_client_id": 9,
            "release_guid": "g",
            "indexer_id": 1,
            "release_title": "Movie.2024.1080p.WEB-DL-GRP",
        },
        headers=plex_headers,
    )

    assert response.status_code == 403
    assert not grab_route.called
    assert (await db.execute(select(Grab))).scalars().first() is None


@respx.mock
async def test_manager_grab_requires_a_download_client_id(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)

    response = await client.post(
        "/manager/grab",
        json={
            "release_guid": "g",
            "indexer_id": 1,
            "release_title": "Movie.2024.1080p.WEB-DL-GRP",
        },
        headers=plex_headers,
    )
    assert response.status_code == 422


@respx.mock
async def test_manager_grab_rejects_an_action_id(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # The tvOS action-scoped grab is a different endpoint now (POST /grab);
    # this one only ever takes a download_client_id.
    mock_seerr_auth(permissions=2)

    response = await client.post(
        "/manager/grab",
        json={
            "action_id": 1,
            "download_client_id": 9,
            "release_guid": "g",
            "indexer_id": 1,
            "release_title": "Movie.2024.1080p.WEB-DL-GRP",
        },
        headers=plex_headers,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Upstream failures on the live-validating paths
#
# /manager/grab and /manager/download-clients validate against Seerr on every
# call rather than reading the token mapping, so each has to keep "Seerr says
# no" (401) distinct from "Seerr did not answer" (502): a client that treats an
# outage as a bad token throws away a working Plex token and cannot recover
# without a fresh sign-in.
# --------------------------------------------------------------------------- #


@respx.mock
async def test_manager_grab_401s_when_seerr_rejects_the_token(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    seerr_rejects_the_token()
    prowlarr = respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )

    response = await client.post("/manager/grab", json=DIRECT_GRAB_BODY, headers=plex_headers)

    assert response.status_code == 401
    assert not prowlarr.called
    assert (await db.execute(select(Grab))).scalars().first() is None


@respx.mock
async def test_manager_grab_502s_when_seerr_is_unreachable(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Not 401: the token may be perfectly good, and the client must keep it.
    seerr_is_unreachable()
    prowlarr = respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )

    response = await client.post("/manager/grab", json=DIRECT_GRAB_BODY, headers=plex_headers)

    assert response.status_code == 502
    assert not prowlarr.called
    assert (await db.execute(select(Grab))).scalars().first() is None


# --------------------------------------------------------------------------- #
# GET /manager/download-clients
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_request_manager_can_list_download_clients(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    respx.get(f"{PROWLARR_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "qBittorrent"}])
    )

    response = await client.get("/manager/download-clients", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["download_clients"][0]["name"] == "qBittorrent"


@respx.mock
async def test_a_regular_user_cannot_list_download_clients(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=32)
    response = await client.get("/manager/download-clients", headers=plex_headers)
    assert response.status_code == 403


@respx.mock
async def test_manager_download_clients_401s_when_seerr_rejects_the_token(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr_rejects_the_token()
    response = await client.get("/manager/download-clients", headers=plex_headers)
    assert response.status_code == 401


@respx.mock
async def test_manager_download_clients_502s_when_seerr_is_unreachable(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr_is_unreachable()
    response = await client.get("/manager/download-clients", headers=plex_headers)
    assert response.status_code == 502


@respx.mock
async def test_manager_download_clients_502s_when_prowlarr_fails(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    respx.get(f"{PROWLARR_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )

    response = await client.get("/manager/download-clients", headers=plex_headers)

    assert response.status_code == 502
