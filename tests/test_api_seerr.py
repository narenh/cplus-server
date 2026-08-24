"""``/seerr/*`` passthrough tests.

The point of these endpoints is that the Seerr admin API key stops living on
client devices, so the tests that matter most are the ones proving cplus still
holds no Seerr credential, acts as the calling user, and refuses to approve for
a non-admin.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ActivityLog, Config, User

from .conftest import SEERR_URL, seerr_user_payload

ADMIN = 2
MANAGE_REQUESTS = 16
PLAIN_USER = 32

REQUESTS_BODY = {
    "pageInfo": {"pages": 1, "results": 2},
    "results": [
        {"id": 1, "status": 1, "media": {"tmdbId": 693134}},
        {"id": 2, "status": 2, "media": {"tmdbId": 1399}},
    ],
}


def mock_auth(permissions: int = PLAIN_USER, *, user_id: int = 42) -> respx.Route:
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200,
            json=seerr_user_payload(user_id=user_id, permissions=permissions),
            headers={"set-cookie": "connect.sid=sess-abc; Path=/"},
        )
    )


# --------------------------------------------------------------------------- #
# Auth posture
# --------------------------------------------------------------------------- #


@respx.mock
async def test_calls_are_made_as_the_user_not_with_an_api_key(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    route = respx.get(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(200, json=REQUESTS_BODY)
    )

    await client.get("/seerr/requests", headers=plex_headers)

    request = route.calls[0].request
    # The caller's own Seerr session, obtained from their Plex token.
    assert "sess-abc" in request.headers["cookie"]
    # And emphatically not an API key: cplus holds no Seerr credential.
    assert "x-api-key" not in {k.lower() for k in request.headers}


async def test_no_seerr_api_key_exists_in_configuration(db: AsyncSession) -> None:
    # The property these endpoints are meant to preserve, asserted directly.
    columns = set(Config.__table__.columns.keys())
    assert "prowlarr_api_key" in columns
    assert not any("seerr" in name and "key" in name for name in columns)


@respx.mock
async def test_every_seerr_route_requires_a_plex_token(
    client: httpx.AsyncClient, configured: Config
) -> None:
    for method, path in [
        ("GET", "/seerr/me"),
        ("GET", "/seerr/requests"),
        ("POST", "/seerr/requests/1/approve"),
        ("DELETE", "/seerr/requests/1"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 401, path


@respx.mock
async def test_a_rejected_plex_token_is_401_and_an_unreachable_seerr_is_502(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(403, json={"message": "no"})
    )
    assert (await client.get("/seerr/me", headers=plex_headers)).status_code == 401

    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )
    assert (await client.get("/seerr/me", headers=plex_headers)).status_code == 502


# --------------------------------------------------------------------------- #
# fetchCurrentUser
# --------------------------------------------------------------------------- #


@respx.mock
async def test_me_returns_seerrs_body_verbatim(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    body = {"id": 42, "plexUsername": "someone", "permissions": 32, "email": "a@b.c"}
    respx.get(f"{SEERR_URL}/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json=body)
    )

    response = await client.get("/seerr/me", headers=plex_headers)

    assert response.status_code == 200
    # Verbatim: the clients already parse Seerr's shapes.
    assert response.json() == body


# --------------------------------------------------------------------------- #
# fetchAllRequests
# --------------------------------------------------------------------------- #


@respx.mock
async def test_listing_requests_passes_through_body_and_paging(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    route = respx.get(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(200, json=REQUESTS_BODY)
    )

    response = await client.get(
        "/seerr/requests",
        params={"take": 10, "skip": 20, "filter": "pending", "sort": "added"},
        headers=plex_headers,
    )

    assert response.status_code == 200
    assert response.json() == REQUESTS_BODY

    params = route.calls[0].request.url.params
    assert params["take"] == "10"
    assert params["skip"] == "20"
    assert params["filter"] == "pending"
    assert params["sort"] == "added"


@respx.mock
@pytest.mark.parametrize("permissions", [PLAIN_USER, MANAGE_REQUESTS, ADMIN])
async def test_listing_is_allowed_for_everyone_and_scoped_by_seerr(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict, permissions: int
) -> None:
    # cplus does not branch on who is calling: Seerr narrows a plain user to
    # their own requests and shows an admin everything, from one endpoint.
    mock_auth(permissions)
    respx.get(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(200, json=REQUESTS_BODY)
    )

    response = await client.get("/seerr/requests", headers=plex_headers)
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# approve / decline — admin only
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_an_admin_can_decide_a_request(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    decision: str,
) -> None:
    mock_auth(ADMIN, user_id=1)
    route = respx.post(f"{SEERR_URL}/api/v1/request/7/{decision}").mock(
        return_value=httpx.Response(200, json={"id": 7, "status": 2})
    )

    response = await client.post(f"/seerr/requests/7/{decision}", headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["id"] == 7
    assert route.called

    entry = (await db.execute(select(ActivityLog))).scalars().all()[-1]
    assert entry.detail["kind"] == f"request_{decision}"
    assert entry.detail["seerr_request_id"] == 7
    assert entry.detail["success"] is True


@respx.mock
@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_a_regular_user_can_never_decide_a_request(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict, decision: str
) -> None:
    # Refused by cplus before Seerr is asked, so the rule lives in our code
    # rather than being inherited from Seerr's 403.
    mock_auth(PLAIN_USER)
    route = respx.post(f"{SEERR_URL}/api/v1/request/7/{decision}").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )

    response = await client.post(f"/seerr/requests/7/{decision}", headers=plex_headers)

    assert response.status_code == 403
    assert "manage requests" in response.json()["detail"]
    assert not route.called


@respx.mock
async def test_manage_requests_permission_is_enough_without_the_admin_bit(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # Mirrors Seerr's own rule, which guards these on MANAGE_REQUESTS.
    mock_auth(MANAGE_REQUESTS)
    respx.post(f"{SEERR_URL}/api/v1/request/7/approve").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )

    response = await client.post("/seerr/requests/7/approve", headers=plex_headers)
    assert response.status_code == 200


@respx.mock
async def test_an_unknown_decision_is_rejected(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth(ADMIN)
    response = await client.post("/seerr/requests/7/delete-everything", headers=plex_headers)
    assert response.status_code == 422


@respx.mock
async def test_seerrs_own_rejection_is_surfaced(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_auth(ADMIN)
    respx.post(f"{SEERR_URL}/api/v1/request/7/approve").mock(
        return_value=httpx.Response(404, json={"message": "Request not found"})
    )

    response = await client.post("/seerr/requests/7/approve", headers=plex_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Request not found"

    entry = (await db.execute(select(ActivityLog))).scalars().all()[-1]
    assert entry.detail["success"] is False


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


@respx.mock
async def test_deleting_a_request_is_left_to_seerrs_own_rule(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Seerr allows a user to delete their own and an admin to delete any, which
    # is the rule we want, so cplus adds no gate of its own.
    mock_auth(PLAIN_USER)
    route = respx.delete(f"{SEERR_URL}/api/v1/request/9").mock(
        return_value=httpx.Response(204)
    )

    response = await client.delete("/seerr/requests/9", headers=plex_headers)

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert route.called

    entry = (await db.execute(select(ActivityLog))).scalars().all()[-1]
    assert entry.detail["kind"] == "request_delete"


@respx.mock
async def test_deleting_someone_elses_request_is_refused_by_seerr(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth(PLAIN_USER)
    respx.delete(f"{SEERR_URL}/api/v1/request/9").mock(
        return_value=httpx.Response(403, json={"message": "Not your request"})
    )

    response = await client.delete("/seerr/requests/9", headers=plex_headers)

    assert response.status_code == 403
    assert response.json()["detail"] == "Not your request"


# --------------------------------------------------------------------------- #
# The allowlist really is an allowlist
# --------------------------------------------------------------------------- #


@respx.mock
async def test_settings_are_not_reachable_through_the_service(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # The whole point: an owner-account caller must not be able to read
    # Radarr/Sonarr credentials back out through cplus.
    mock_auth(ADMIN, user_id=1)

    for path in (
        "/seerr/settings/main",
        "/seerr/api/v1/settings/main",
        "/seerr/requests/../settings/main",
    ):
        response = await client.get(path, headers=plex_headers)
        assert response.status_code in (404, 405), path


@respx.mock
async def test_the_user_is_upserted_like_any_other_entry_point(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_auth(ADMIN, user_id=77)
    respx.get(f"{SEERR_URL}/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json={"id": 77})
    )

    await client.get("/seerr/me", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    assert user.seerr_user_id == 77


# --------------------------------------------------------------------------- #
# Upstream failures on the read endpoints
#
# /me and /requests only read, so unlike approve/decline they have no
# activity_log row to protect and raise rather than return. What still has to
# hold is that Seerr's own 4xx reaches the client intact — the admin app shows
# that wording — while anything else becomes a 502.
# --------------------------------------------------------------------------- #


@respx.mock
async def test_me_surfaces_seerrs_own_rejection(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    respx.get(f"{SEERR_URL}/api/v1/auth/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )

    response = await client.get("/seerr/me", headers=plex_headers)

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


@respx.mock
async def test_me_reports_an_upstream_fault_as_502(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    respx.get(f"{SEERR_URL}/api/v1/auth/me").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )

    response = await client.get("/seerr/me", headers=plex_headers)

    assert response.status_code == 502


@respx.mock
async def test_listing_requests_surfaces_seerrs_own_rejection(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    respx.get(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(403, json={"message": "Not allowed"})
    )

    response = await client.get("/seerr/requests", headers=plex_headers)

    assert response.status_code == 403
    assert response.json()["detail"] == "Not allowed"


@respx.mock
async def test_listing_requests_reports_an_upstream_fault_as_502(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_auth()
    respx.get(f"{SEERR_URL}/api/v1/request").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    response = await client.get("/seerr/requests", headers=plex_headers)

    assert response.status_code == 502
