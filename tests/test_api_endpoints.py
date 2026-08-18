"""Endpoint tests for /search, /grab and /request."""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Action, ActivityLog, Config, EventType, Grab, User

from .conftest import (
    PROWLARR_URL,
    SEERR_URL,
    grant,
    make_action,
    seerr_user_payload,
)

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


async def authenticate(client: httpx.AsyncClient, headers: dict) -> None:
    """Run the tvOS checkpoint so the Plex-token cache is populated."""
    response = await client.get("/actions", headers=headers)
    assert response.status_code == 200


def ndjson(response: httpx.Response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# GET /search
# --------------------------------------------------------------------------- #


@respx.mock
async def test_search_requires_a_cached_token(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_prowlarr_search([WEB_2160])
    response = await client.get(
        "/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )
    assert response.status_code == 401
    assert "GET /actions" in response.json()["detail"]


@respx.mock
async def test_search_makes_no_outbound_auth_call(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr = mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)
    calls_after_auth = seerr.call_count

    await client.get("/search", params={"imdb_id": "tt0111161"}, headers=plex_headers)

    # Cache-only: search must not touch Plex or Seerr.
    assert seerr.call_count == calls_after_auth


@respx.mock
async def test_search_streams_ndjson_with_tagged_releases(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160, FULL_DISC, WEB_1080])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)

    response = await client.get(
        "/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = ndjson(response)
    assert [line["phase"] for line in lines] == ["all"]

    guids = [r["guid"] for r in lines[0]["releases"]]
    assert guids == ["guid-uhd", "guid-fhd"]  # full disc dropped by the parser
    assert lines[0]["releases"][0]["has_atmos"] is True
    assert lines[0]["recommendations"] == {str(action.id): "guid-uhd"}


@respx.mock
async def test_search_streams_two_phases_when_a_preferred_indexer_is_set(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    configured.preferred_indexer_id = 1
    db.add(configured)
    await db.commit()

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)

    def by_indexer(request: httpx.Request) -> httpx.Response:
        scoped = request.url.params.get("indexerIds")
        return httpx.Response(200, json=[WEB_2160] if scoped else [WEB_2160, WEB_1080])

    respx.get(f"{PROWLARR_URL}/api/v1/search").mock(side_effect=by_indexer)

    response = await client.get(
        "/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )

    lines = ndjson(response)
    assert [line["phase"] for line in lines] == ["preferred", "all"]
    assert [r["guid"] for r in lines[0]["releases"]] == ["guid-uhd"]
    assert [r["guid"] for r in lines[1]["releases"]] == ["guid-fhd"]
    # Preferred subset was non-empty, so the answer is unchanged in phase 2.
    assert lines[1]["recommendations"] == {str(action.id): "guid-uhd"}


@respx.mock
async def test_search_excludes_the_request_action_from_recommendations(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    request_action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    grab_action = await make_action(db, "Stream Now")
    await grant(db, user, request_action)
    await grant(db, user, grab_action)

    response = await client.get(
        "/search", params={"imdb_id": "tt0111161"}, headers=plex_headers
    )

    # Request has no quality profile and never touches Prowlarr.
    assert ndjson(response)[0]["recommendations"] == {str(grab_action.id): "guid-uhd"}


@respx.mock
async def test_search_with_no_results_still_completes_the_stream(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([])
    await authenticate(client, plex_headers)

    response = await client.get(
        "/search", params={"imdb_id": "tt0000000"}, headers=plex_headers
    )

    lines = ndjson(response)
    assert lines == [{"phase": "all", "releases": [], "recommendations": {}}]


@respx.mock
async def test_search_rejects_non_movie_types(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    response = await client.get(
        "/search", params={"imdb_id": "tt1", "type": "tv"}, headers=plex_headers
    )
    assert response.status_code == 422


@respx.mock
async def test_search_is_logged_to_the_activity_log(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    await client.get("/search", params={"imdb_id": "tt0111161"}, headers=plex_headers)

    entry = (
        await db.execute(
            select(ActivityLog).where(ActivityLog.event_type == EventType.SEARCH)
        )
    ).scalar_one()
    assert entry.detail["imdb_id"] == "tt0111161"


async def test_search_before_prowlarr_is_configured_is_503(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    response = await client.get(
        "/search", params={"imdb_id": "tt1"}, headers=plex_headers
    )
    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# POST /grab
# --------------------------------------------------------------------------- #


@respx.mock
async def test_grab_sends_the_release_to_the_actions_download_client(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    grab_route = respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now", download_client_id=9)
    await grant(db, user, action)
    await client.get("/search", params={"imdb_id": "tt0111161"}, headers=plex_headers)

    response = await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "guid-uhd", "indexer_id": 1},
        headers=plex_headers,
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert json.loads(grab_route.calls[0].request.content) == {
        "guid": "guid-uhd",
        "indexerId": 1,
        "downloadClientId": 9,
    }


@respx.mock
async def test_grab_enriches_the_history_row_from_the_search_cache(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)
    await client.get("/search", params={"imdb_id": "tt0111161"}, headers=plex_headers)

    await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "guid-uhd", "indexer_id": 1},
        headers=plex_headers,
    )

    record = (await db.execute(select(Grab))).scalar_one()
    # The client never sent these; they came from the cache.
    assert record.release_title == WEB_2160["title"]
    assert record.size_bytes == 25 * GB
    assert record.user_id == user.id


@respx.mock
async def test_a_cache_miss_degrades_the_history_but_still_grabs(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={})
    )
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)

    # No /search first, so nothing is cached — as after a restart.
    response = await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "guid-unknown", "indexer_id": 3},
        headers=plex_headers,
    )

    assert response.status_code == 200
    record = (await db.execute(select(Grab))).scalar_one()
    assert record.release_title is None
    assert record.indexer_id == 3


@respx.mock
async def test_grab_refuses_an_action_the_user_lacks_permission_for(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)
    action = await make_action(db, "Add 4K")  # never granted

    response = await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "g", "indexer_id": 1},
        headers=plex_headers,
    )

    assert response.status_code == 403
    assert (await db.execute(select(Grab))).scalars().first() is None


@respx.mock
async def test_grab_refuses_an_unknown_action_the_same_way(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    response = await client.post(
        "/grab",
        json={"action_id": 9999, "release_guid": "g", "indexer_id": 1},
        headers=plex_headers,
    )
    assert response.status_code == 403


@respx.mock
async def test_grab_rejects_the_request_action(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    request_action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, user, request_action)

    response = await client.post(
        "/grab",
        json={"action_id": request_action.id, "release_guid": "g", "indexer_id": 1},
        headers=plex_headers,
    )

    assert response.status_code == 400
    assert "POST /request" in response.json()["detail"]


@respx.mock
async def test_a_prowlarr_failure_is_reported_and_logged(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(500, text="boom")
    )
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)

    response = await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "g", "indexer_id": 1},
        headers=plex_headers,
    )

    assert response.status_code == 502
    assert response.json()["success"] is False
    assert (await db.execute(select(Grab))).scalars().first() is None

    entry = (
        await db.execute(
            select(ActivityLog).where(ActivityLog.event_type == EventType.GRAB)
        )
    ).scalar_one()
    assert entry.detail["success"] is False


async def test_grab_requires_a_cached_token(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    response = await client.post(
        "/grab",
        json={"action_id": 1, "release_guid": "g", "indexer_id": 1},
        headers=plex_headers,
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# POST /request
# --------------------------------------------------------------------------- #


async def grant_request_action(client, db, plex_headers) -> Action:  # noqa: ANN001
    await authenticate(client, plex_headers)
    user = (await db.execute(select(User))).scalar_one()
    action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, user, action)
    return action


@respx.mock
async def test_request_creates_a_movie_request_in_seerr(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    route = respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 77, "status": 1})
    )
    await grant_request_action(client, db, plex_headers)

    response = await client.post(
        "/request", json={"tmdb_id": 693134, "type": "movie"}, headers=plex_headers
    )

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": None, "request_id": 77}
    # TMDB id, not IMDB id, and no seasons for a movie.
    assert json.loads(route.calls[0].request.content) == {
        "mediaType": "movie",
        "mediaId": 693134,
    }


@respx.mock
async def test_request_passes_tv_seasons_through_verbatim(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    route = respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 78})
    )
    await grant_request_action(client, db, plex_headers)

    response = await client.post(
        "/request",
        json={"tmdb_id": 1399, "type": "tv", "seasons": [0, 1, 2]},
        headers=plex_headers,
    )

    assert response.status_code == 200
    # Season 0 is specials; we never substitute the literal "all", which would
    # silently drop them.
    assert json.loads(route.calls[0].request.content) == {
        "mediaType": "tv",
        "mediaId": 1399,
        "seasons": [0, 1, 2],
    }


@respx.mock
async def test_request_rejects_tv_without_seasons(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await grant_request_action(client, db, plex_headers)

    for body in ({"tmdb_id": 1399, "type": "tv"}, {"tmdb_id": 1399, "type": "tv", "seasons": []}):
        response = await client.post("/request", json=body, headers=plex_headers)
        assert response.status_code == 422, body


@respx.mock
async def test_request_rejects_seasons_on_a_movie(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await grant_request_action(client, db, plex_headers)

    response = await client.post(
        "/request",
        json={"tmdb_id": 693134, "type": "movie", "seasons": [1]},
        headers=plex_headers,
    )
    assert response.status_code == 422


@respx.mock
async def test_request_acts_as_the_user_via_the_seerr_session_cookie(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200,
            json=seerr_user_payload(),
            headers={"set-cookie": "connect.sid=seerr-session-xyz; Path=/; HttpOnly"},
        )
    )
    route = respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    await grant_request_action(client, db, plex_headers)

    await client.post(
        "/request", json={"tmdb_id": 5, "type": "movie"}, headers=plex_headers
    )

    assert "seerr-session-xyz" in route.calls[0].request.headers["cookie"]


@respx.mock
async def test_request_always_validates_live_rather_than_using_the_cache(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    auth_route = mock_seerr_auth()
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    await grant_request_action(client, db, plex_headers)
    calls_before = auth_route.call_count

    await client.post(
        "/request", json={"tmdb_id": 5, "type": "movie"}, headers=plex_headers
    )

    assert auth_route.call_count == calls_before + 1


@respx.mock
async def test_request_surfaces_seerrs_own_rejection_reason(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(403, json={"message": "Request quota exceeded"})
    )
    await grant_request_action(client, db, plex_headers)

    response = await client.post(
        "/request", json={"tmdb_id": 5, "type": "movie"}, headers=plex_headers
    )

    assert response.status_code == 403
    assert response.json() == {
        "success": False,
        "message": "Request quota exceeded",
        "request_id": None,
    }


@respx.mock
async def test_request_without_permission_is_403(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)  # authenticated but not granted

    response = await client.post(
        "/request", json={"tmdb_id": 5, "type": "movie"}, headers=plex_headers
    )
    assert response.status_code == 403


@respx.mock
async def test_request_writes_activity_log_but_no_grab_row(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )
    await grant_request_action(client, db, plex_headers)

    await client.post(
        "/request",
        json={"tmdb_id": 1399, "type": "tv", "seasons": [1]},
        headers=plex_headers,
    )

    # Nothing was grabbed via Prowlarr, so there is no grabs row.
    assert (await db.execute(select(Grab))).scalars().first() is None

    entries = (
        await db.execute(
            select(ActivityLog).where(ActivityLog.event_type == EventType.GRAB)
        )
    ).scalars().all()
    assert len(entries) == 1
    assert entries[0].detail["kind"] == "request"
    assert entries[0].detail["seerr_request_id"] == 99
    assert entries[0].detail["seasons"] == [1]


# --------------------------------------------------------------------------- #
# Admin stubs
# --------------------------------------------------------------------------- #


async def test_admin_routes_are_stubbed_for_stage_three(client: httpx.AsyncClient) -> None:
    for path in ("/admin/config", "/admin/prowlarr/indexers", "/admin/activity-log"):
        response = await client.get(path)
        assert response.status_code == 501, path
        assert "stage 3" in response.json()["detail"]


async def test_health_is_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
