"""``GET /titles/{imdb_id}/actions`` — the movie-detail-page call.

Split out from the plain-search tests: this endpoint replaced
``GET /search?imdb_id=...``, and its response shape (an ``actions`` array,
each with a ``kind`` and its own recommended release) is different enough from
free-text search's bare ``recommendations`` dict to deserve its own file.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import (
    Action,
    ActivityLog,
    Config,
    EventType,
    QualityProfile,
    User,
)

from .conftest import PROWLARR_URL, SEERR_URL, grant, make_action, seerr_user_payload

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
    response = await client.get("/register", headers=headers)
    assert response.status_code == 200


def ndjson(response: httpx.Response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@respx.mock
async def test_titles_requires_a_cached_token(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_prowlarr_search([WEB_2160])
    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)
    assert response.status_code == 401
    assert "GET /register" in response.json()["detail"]


@respx.mock
async def test_titles_makes_no_outbound_auth_call(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    seerr = mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)
    calls_after_auth = seerr.call_count

    await client.get("/titles/tt0111161/actions", headers=plex_headers)

    # Cache-only: this must not touch Plex or Seerr, same as /search.
    assert seerr.call_count == calls_after_auth


@respx.mock
async def test_titles_streams_ndjson_with_action_offers(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160, FULL_DISC, WEB_1080])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = ndjson(response)
    assert [line["phase"] for line in lines] == ["all"]

    guids = [r["guid"] for r in lines[0]["releases"]]
    assert guids == ["guid-uhd", "guid-fhd"]  # full disc dropped by the parser
    assert lines[0]["releases"][0]["has_atmos"] is True
    assert lines[0]["actions"] == [
        {
            "id": action.id,
            "name": "Stream Now",
            # No display title configured, so the button copy is the name.
            "display_title": "Stream Now",
            "kind": "grab",
            "recommended_release_guid": "guid-uhd",
        }
    ]


@respx.mock
async def test_titles_streams_two_phases_when_a_preferred_indexer_is_set(
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

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    lines = ndjson(response)
    assert [line["phase"] for line in lines] == ["preferred", "all"]
    assert [r["guid"] for r in lines[0]["releases"]] == ["guid-uhd"]
    assert [r["guid"] for r in lines[1]["releases"]] == ["guid-fhd"]
    # Preferred subset was non-empty, so the answer is unchanged in phase 2.
    assert lines[1]["actions"][0]["recommended_release_guid"] == "guid-uhd"


@respx.mock
async def test_titles_includes_the_request_action_without_a_recommendation(
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

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    actions = {a["id"]: a for a in ndjson(response)[0]["actions"]}
    # Request has no quality profile and never touches Prowlarr, but it is
    # still reported — just with no recommendation, not omitted.
    assert actions[request_action.id] == {
        "id": request_action.id,
        "name": "Request",
        "display_title": "Request",
        "kind": "request",
        "recommended_release_guid": None,
    }
    assert actions[grab_action.id]["kind"] == "grab"
    assert actions[grab_action.id]["recommended_release_guid"] == "guid-uhd"


@respx.mock
async def test_an_actions_recommendation_honours_the_profiles_choices(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # The whole path, end to end: a profile's choices have to reach the engine
    # attached to it, or the button recommends one release while the admin page
    # insists on another. The 4K copy is listed first and would win on its own,
    # so only the choices can produce the answer below.
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160, WEB_1080])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    profile = await db.get(QualityProfile, action.quality_profile_id)
    assert profile is not None
    profile.choices = [
        {"match": {"resolutions": ["1080p"]}, "tie_break": "biggest"},
        {"match": {"resolutions": ["2160p"]}, "tie_break": "biggest"},
    ]
    await db.commit()
    await grant(db, user, action)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    # 1080p is the smaller, second-listed release; only the choices put it first.
    assert ndjson(response)[0]["actions"][0]["recommended_release_guid"] == "guid-fhd"


# --------------------------------------------------------------------------- #
# Button copy
#
# The admin's name for an action and the words on the button are two different
# things: an admin may file something as "Add to library in HD" while the
# person holding the remote should just see "Play Now".
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_display_title_becomes_the_button_copy_without_changing_the_name(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Add to library in HD")
    action.display_title = "Play Now"
    await db.commit()
    await grant(db, user, action)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    offer = ndjson(response)[0]["actions"][0]
    assert offer["display_title"] == "Play Now"
    # The name is unchanged: it is what the admin UI, the grab history and the
    # notification text call this action.
    assert offer["name"] == "Add to library in HD"


@respx.mock
async def test_the_request_action_is_told_apart_by_kind_not_by_its_words(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Both its name and its button copy are the admin's to change, so a client
    # that matched on either would break the first time one did. `kind` is the
    # field that says where pressing this button posts.
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    request_action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    request_action.name = "Ask the household"
    request_action.display_title = "Ask for this"
    await db.commit()
    await grant(db, user, request_action)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    offer = ndjson(response)[0]["actions"][0]
    assert offer["name"] == "Ask the household"
    assert offer["display_title"] == "Ask for this"
    assert offer["kind"] == "request"


# --------------------------------------------------------------------------- #
# Holding no Prowlarr-backed action grants no Prowlarr access
#
# Actions are the only way a regular user is granted access to Prowlarr's
# indexers. Unrestricted search, independent of holding any action, is the
# admin app's job at GET /manager/search instead — see test_manager.py.
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_user_with_no_actions_never_triggers_a_prowlarr_search(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    route = mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    assert response.status_code == 200
    assert not route.called
    assert ndjson(response) == [{"phase": "all", "releases": [], "actions": []}]


@respx.mock
async def test_a_user_with_only_the_request_action_never_triggers_a_search(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Request never touches Prowlarr — holding only it is not "having an
    # action", in the sense of Prowlarr access.
    mock_seerr_auth()
    route = mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    request_action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, user, request_action)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    assert response.status_code == 200
    assert not route.called
    assert ndjson(response) == [
        {
            "phase": "all",
            "releases": [],
            "actions": [
                {
                    "id": request_action.id,
                    "name": "Request",
                    "display_title": "Request",
                    "kind": "request",
                    "recommended_release_guid": None,
                }
            ],
        }
    ]


@respx.mock
async def test_titles_with_no_results_still_completes_the_stream(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([])
    await authenticate(client, plex_headers)

    response = await client.get("/titles/tt0000000/actions", headers=plex_headers)

    lines = ndjson(response)
    assert lines == [{"phase": "all", "releases": [], "actions": []}]


@respx.mock
async def test_preferred_only_scopes_titles_to_one_indexer(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    await grant(db, user, await make_action(db, "Stream Now"))

    configured.preferred_indexer_id = 1
    db.add(configured)
    await db.commit()

    route = mock_prowlarr_search([WEB_2160])
    response = await client.get(
        "/titles/tt0111161/actions",
        params={"preferred_only": "true"},
        headers=plex_headers,
    )

    assert response.status_code == 200
    assert [line["phase"] for line in ndjson(response)] == ["all"]
    assert route.call_count == 1
    assert route.calls[0].request.url.params.get_list("indexerIds") == ["1"]


@respx.mock
async def test_the_titles_default_is_all_indexers(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    await grant(db, user, await make_action(db, "Stream Now"))

    configured.preferred_indexer_id = 1
    db.add(configured)
    await db.commit()

    def by_indexer(request: httpx.Request) -> httpx.Response:
        scoped = request.url.params.get("indexerIds")
        return httpx.Response(200, json=[WEB_2160] if scoped else [WEB_2160, WEB_1080])

    respx.get(f"{PROWLARR_URL}/api/v1/search").mock(side_effect=by_indexer)

    response = await client.get("/titles/tt0111161/actions", headers=plex_headers)

    # No preferred_only: the unscoped search still happens, so the client ends
    # up with every indexer's results.
    assert [line["phase"] for line in ndjson(response)] == ["preferred", "all"]


@respx.mock
async def test_preferred_only_without_a_preferred_indexer_searches_everything(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    route = mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    await grant(db, user, await make_action(db, "Stream Now"))

    response = await client.get(
        "/titles/tt0111161/actions",
        params={"preferred_only": "true"},
        headers=plex_headers,
    )

    assert response.status_code == 200
    assert "indexerIds" not in route.calls[0].request.url.params


@respx.mock
async def test_titles_is_logged_to_the_activity_log(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    mock_prowlarr_search([WEB_2160])
    await authenticate(client, plex_headers)

    await client.get("/titles/tt0111161/actions", headers=plex_headers)

    entry = (
        await db.execute(
            select(ActivityLog).where(ActivityLog.event_type == EventType.SEARCH)
        )
    ).scalar_one()
    assert entry.detail["imdb_id"] == "tt0111161"


async def test_titles_before_prowlarr_is_configured_is_503(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    response = await client.get("/titles/tt1/actions", headers=plex_headers)
    assert response.status_code == 503


@respx.mock
async def test_any_live_seerr_call_primes_the_mapping_for_titles(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # The admin app never calls /actions — that endpoint is tvOS-only — so its
    # first /seerr/* call has to be what makes this endpoint work.
    mock_seerr_auth(permissions=2)
    respx.get(f"{SEERR_URL}/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )
    mock_prowlarr_search([WEB_2160])

    before = await client.get("/titles/tt1/actions", headers=plex_headers)
    assert before.status_code == 401

    await client.get("/seerr/me", headers=plex_headers)

    after = await client.get("/titles/tt1/actions", headers=plex_headers)
    assert after.status_code == 200
    # No actions granted, so nothing is offered.
    assert ndjson(after)[0]["actions"] == []
