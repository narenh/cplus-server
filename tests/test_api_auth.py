"""Auth flow tests — the tvOS token/cache path and the webui PIN/session path.

The two flows are deliberately not interchangeable, and several tests here exist
specifically to prove they have not been conflated.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.auth.sessions import SESSION_COOKIE_NAME
from cplus_service.db.models import Action, Config, User

from .conftest import (
    PLEX_TOKEN,
    SEERR_URL,
    grant,
    make_action,
    seerr_user_payload,
)

ADMIN_PERMISSIONS = 2
PLAIN_USER_PERMISSIONS = 32


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


# --------------------------------------------------------------------------- #
# GET /actions — the tvOS checkpoint
# --------------------------------------------------------------------------- #


@respx.mock
async def test_actions_validates_against_seerr_and_creates_the_user(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    route = mock_seerr_auth(user_id=42, username="alice")

    response = await client.get("/actions", headers=plex_headers)

    assert response.status_code == 200
    assert route.called
    assert route.calls[0].request.content == b'{"authToken":"plex-token-abc"}'

    user = (await db.execute(select(User))).scalar_one()
    assert user.seerr_user_id == 42
    assert user.plex_username == "alice"


@respx.mock
async def test_actions_returns_only_id_and_name(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    stream_now = await make_action(db, "Stream Now")
    await make_action(db, "Add 4K")  # not granted
    await grant(db, user, stream_now)

    response = await client.get("/actions", headers=plex_headers)

    assert response.json() == {"actions": [{"id": stream_now.id, "name": "Stream Now"}]}


@respx.mock
async def test_actions_includes_the_built_in_request_action_when_granted(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    request_action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, user, request_action)

    response = await client.get("/actions", headers=plex_headers)

    # The client routes on this name, which is safe because a system action
    # cannot be renamed.
    assert response.json()["actions"] == [{"id": request_action.id, "name": "Request"}]


@respx.mock
async def test_actions_upserts_rather_than_duplicating_on_every_launch(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(user_id=42, username="alice")
    await client.get("/actions", headers=plex_headers)
    await client.get("/actions", headers=plex_headers)

    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1


@respx.mock
async def test_a_renamed_plex_account_refreshes_the_cached_username(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(user_id=42, username="alice")
    await client.get("/actions", headers=plex_headers)

    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(user_id=42, username="alicia"))
    )
    await client.get("/actions", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    assert user.plex_username == "alicia"


async def test_actions_without_a_plex_token_is_401(
    client: httpx.AsyncClient, configured: Config
) -> None:
    assert (await client.get("/actions")).status_code == 401


@respx.mock
async def test_a_rejected_plex_token_is_401(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(403, json={"message": "Unauthorized"})
    )

    response = await client.get("/actions", headers=plex_headers)
    assert response.status_code == 401


@respx.mock
async def test_an_unreachable_seerr_is_502_not_401(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # A 401 here would make the client discard a perfectly good Plex token.
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )

    response = await client.get("/actions", headers=plex_headers)
    assert response.status_code == 502


async def test_actions_before_seerr_is_configured_is_503(
    client: httpx.AsyncClient, plex_headers: dict
) -> None:
    assert (await client.get("/actions", headers=plex_headers)).status_code == 503


# --------------------------------------------------------------------------- #
# The Plex-token cache
# --------------------------------------------------------------------------- #


@respx.mock
async def test_actions_populates_the_cache_that_search_and_grab_rely_on(
    app: FastAPI, client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    assert await app.state.cplus.plex_cache.size() == 0

    await client.get("/actions", headers=plex_headers)

    assert await app.state.cplus.plex_cache.size() == 1
    cached = await app.state.cplus.plex_cache.get(PLEX_TOKEN)
    assert cached is not None
    assert cached.seerr_user_id == 42


@respx.mock
async def test_the_cache_never_stores_the_raw_plex_token(
    app: FastAPI, client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)

    stored_keys = list(app.state.cplus.plex_cache._entries)  # noqa: SLF001
    assert PLEX_TOKEN not in stored_keys
    assert all(len(key) == 64 for key in stored_keys)


# --------------------------------------------------------------------------- #
# POST /auth — the webui flow
# --------------------------------------------------------------------------- #


@respx.mock
async def test_auth_issues_a_session_for_the_seerr_admin(
    client: httpx.AsyncClient, configured: Config
) -> None:
    mock_seerr_auth(user_id=1, permissions=ADMIN_PERMISSIONS, username="owner")

    response = await client.post("/auth", json={"plex_token": PLEX_TOKEN})

    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert SESSION_COOKIE_NAME in response.cookies


@respx.mock
async def test_auth_rejects_a_non_admin_with_a_clear_message(
    client: httpx.AsyncClient, configured: Config
) -> None:
    mock_seerr_auth(permissions=PLAIN_USER_PERMISSIONS)

    response = await client.post("/auth", json={"plex_token": PLEX_TOKEN})

    assert response.status_code == 403
    assert "not the Seerr admin" in response.json()["detail"]
    assert SESSION_COOKIE_NAME not in response.cookies


@respx.mock
@pytest.mark.parametrize("permissions", [2, 2 | 32, 2 | 16 | 32])
async def test_the_admin_bit_is_checked_as_a_bitmask(
    client: httpx.AsyncClient, configured: Config, permissions: int
) -> None:
    mock_seerr_auth(user_id=9, permissions=permissions)
    response = await client.post("/auth", json={"plex_token": PLEX_TOKEN})
    assert response.status_code == 200


@respx.mock
async def test_seerr_user_id_1_is_not_treated_as_admin_without_the_bit(
    client: httpx.AsyncClient, configured: Config
) -> None:
    # Admin-ness comes from the bitmask, never from being user 1.
    mock_seerr_auth(user_id=1, permissions=PLAIN_USER_PERMISSIONS)
    response = await client.post("/auth", json={"plex_token": PLEX_TOKEN})
    assert response.status_code == 403


@respx.mock
async def test_first_run_bootstrap_persists_the_supplied_seerr_url(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # No config row at all: the admin cannot set the URL without a session, and
    # cannot get a session without the URL, so /auth accepts it inline.
    mock_seerr_auth(user_id=1, permissions=ADMIN_PERMISSIONS)

    response = await client.post(
        "/auth", json={"plex_token": PLEX_TOKEN, "seerr_url": SEERR_URL}
    )

    assert response.status_code == 200
    config = (await db.execute(select(Config))).scalar_one()
    assert config.seerr_url == SEERR_URL


@respx.mock
async def test_a_seerr_url_that_fails_to_authenticate_is_not_persisted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )

    response = await client.post(
        "/auth", json={"plex_token": PLEX_TOKEN, "seerr_url": SEERR_URL}
    )

    assert response.status_code == 502
    config = (await db.execute(select(Config))).scalars().first()
    assert config is None or config.seerr_url is None


async def test_auth_without_any_seerr_url_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth", json={"plex_token": PLEX_TOKEN})
    assert response.status_code == 400


@respx.mock
async def test_logout_clears_the_session(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    mock_seerr_auth(user_id=1, permissions=ADMIN_PERMISSIONS)
    await client.post("/auth", json={"plex_token": PLEX_TOKEN})

    response = await client.post("/auth/logout")

    assert response.status_code == 200
    from cplus_service.auth.sessions import count_sessions

    assert await count_sessions(db) == 0


# --------------------------------------------------------------------------- #
# The two flows stay separate
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_webui_session_does_not_authenticate_the_tvos_endpoints(
    client: httpx.AsyncClient, configured: Config
) -> None:
    mock_seerr_auth(user_id=1, permissions=ADMIN_PERMISSIONS)
    await client.post("/auth", json={"plex_token": PLEX_TOKEN})

    # Cookie is set on the client, but /search wants a Plex token header.
    response = await client.get("/search", params={"imdb_id": "tt0111161"})
    assert response.status_code == 401


@respx.mock
async def test_the_tvos_flow_issues_no_session_cookie(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    response = await client.get("/actions", headers=plex_headers)
    assert SESSION_COOKIE_NAME not in response.cookies
