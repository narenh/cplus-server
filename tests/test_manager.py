"""``POST /manager/grab``, ``GET /manager/download-clients``, ``GET /manager/search``.

The admin app's request-manager endpoints: live Seerr auth, gated on
``MANAGE_REQUESTS``, no action involved. ``grab`` and ``download-clients`` were
split out of ``POST /grab`` and ``GET /download-clients`` — naming a download
client directly was one of two branches on the tvOS grab endpoint. ``search``
is the unrestricted-search counterpart to ``GET /titles/{imdb_id}/actions``:
regular tvOS users only ever see Prowlarr results through an action they hold,
so unrestricted search — by free text or by IMDB id, independent of holding
any action — lives here instead.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ActivityLog, Config, EventType, Grab

from .conftest import PROWLARR_URL, SEERR_URL, seerr_user_payload

GB = 1024**3

WEB_2160 = {
    "title": "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
    "guid": "guid-uhd",
    "indexerId": 1,
    "indexer": "Tracker One",
    "size": 25 * GB,
}
WEB_1080 = {
    "title": "Movie.2024.1080p.WEB-DL.DDP5.1-GRP",
    "guid": "guid-fhd",
    "indexerId": 2,
    "indexer": "Tracker Two",
    "size": 8 * GB,
}
FULL_DISC = {
    "title": "Movie.2024.COMPLETE.UHD.BLURAY-TERMiNAL",
    "guid": "guid-disc",
    "indexerId": 1,
    "size": 80 * GB,
}


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


def mock_prowlarr_search(results: list[dict]) -> respx.Route:
    return respx.get(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=results)
    )


def ndjson(response: httpx.Response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


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


# --------------------------------------------------------------------------- #
# GET /manager/search
#
# The only way to search Prowlarr independent of holding an action. Regular
# tvOS users never reach this — GET /titles/{imdb_id}/actions is their only
# window onto Prowlarr, and only when they hold a grab-capable action.
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_regular_user_cannot_search(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=32)
    route = mock_prowlarr_search([WEB_2160])

    response = await client.get(
        "/manager/search", params={"query": "the office"}, headers=plex_headers
    )

    assert response.status_code == 403
    assert not route.called


@respx.mock
async def test_a_request_manager_can_search_by_free_text(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    route = mock_prowlarr_search([WEB_2160, WEB_1080])

    response = await client.get(
        "/manager/search", params={"query": "the office"}, headers=plex_headers
    )

    assert response.status_code == 200
    lines = ndjson(response)
    assert [line["phase"] for line in lines] == ["all"]
    assert [r["guid"] for r in lines[0]["releases"]] == ["guid-uhd", "guid-fhd"]
    # Never scored: there is no action here to score against.
    assert lines[0]["recommendations"] == {}

    params = route.calls[0].request.url.params
    assert params["query"] == "the office"
    assert params["type"] == "search"
    # Not category-scoped: TV and anything else Prowlarr indexes can come back.
    assert "categories" not in params


@respx.mock
async def test_a_free_text_result_is_still_parsed_and_full_disc_filtered(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    mock_prowlarr_search([WEB_2160, FULL_DISC])

    response = await client.get(
        "/manager/search", params={"query": "dune"}, headers=plex_headers
    )

    releases = ndjson(response)[0]["releases"]
    assert [r["guid"] for r in releases] == ["guid-uhd"]
    assert releases[0]["has_atmos"] is True


@respx.mock
async def test_a_request_manager_can_search_by_imdb_id(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # Unlike GET /titles/{imdb_id}/actions, this never scores — there is no
    # action to score against, only a release list to hand-pick a grab from.
    mock_seerr_auth(permissions=2)
    mock_prowlarr_search([WEB_2160])

    response = await client.get(
        "/manager/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )

    assert response.status_code == 200
    lines = ndjson(response)
    assert [r["guid"] for r in lines[0]["releases"]] == ["guid-uhd"]
    assert lines[0]["recommendations"] == {}


@respx.mock
async def test_manager_search_streams_two_phases_when_a_preferred_indexer_is_set(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    configured.preferred_indexer_id = 1
    db.add(configured)
    await db.commit()

    def by_indexer(request: httpx.Request) -> httpx.Response:
        scoped = request.url.params.get("indexerIds")
        return httpx.Response(200, json=[WEB_2160] if scoped else [WEB_2160, WEB_1080])

    respx.get(f"{PROWLARR_URL}/api/v1/search").mock(side_effect=by_indexer)

    response = await client.get(
        "/manager/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )

    assert [line["phase"] for line in ndjson(response)] == ["preferred", "all"]


@respx.mock
async def test_manager_search_requires_exactly_one_of_imdb_id_or_query(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)

    neither = await client.get("/manager/search", headers=plex_headers)
    assert neither.status_code == 400

    both = await client.get(
        "/manager/search",
        params={"imdb_id": "tt1", "query": "dune"},
        headers=plex_headers,
    )
    assert both.status_code == 400


@respx.mock
async def test_manager_search_is_logged_to_the_activity_log(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=2)
    mock_prowlarr_search([WEB_2160])

    await client.get(
        "/manager/search",
        params={"query": "dune part two", "preferred_only": "true"},
        headers=plex_headers,
    )

    entry = (
        await db.execute(
            select(ActivityLog).where(ActivityLog.event_type == EventType.SEARCH)
        )
    ).scalar_one()
    assert entry.detail["query"] == "dune part two"
    assert entry.detail["preferred_only"] is True


async def test_manager_search_before_prowlarr_is_configured_is_503(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    response = await client.get(
        "/manager/search", params={"query": "dune"}, headers=plex_headers
    )
    assert response.status_code == 503


@respx.mock
async def test_manager_search_401s_when_seerr_rejects_the_token(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr_rejects_the_token()
    route = mock_prowlarr_search([WEB_2160])

    response = await client.get(
        "/manager/search", params={"query": "dune"}, headers=plex_headers
    )

    assert response.status_code == 401
    assert not route.called


@respx.mock
async def test_manager_search_502s_when_seerr_is_unreachable(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr_is_unreachable()
    route = mock_prowlarr_search([WEB_2160])

    response = await client.get(
        "/manager/search", params={"query": "dune"}, headers=plex_headers
    )

    assert response.status_code == 502
    assert not route.called
