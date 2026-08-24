"""Auth flow tests — the tvOS token/cache path and the webui PIN/session path.

The two flows are deliberately not interchangeable, and several tests here exist
specifically to prove they have not been conflated.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.api.app import create_app
from cplus_service.auth.identity import apply_seerr_url_change
from cplus_service.auth.plex_cache import count_tokens, resolve_token, token_fingerprint
from cplus_service.auth.sessions import SESSION_COOKIE_NAME
from cplus_service.db.models import Action, AdminSession, Config, PlexTokenSession, User

from .conftest import (
    PLEX_TOKEN,
    PROWLARR_URL,
    SEERR_URL,
    grant,
    make_action,
    seerr_user_payload,
)

ADMIN_PERMISSIONS = 2
PLAIN_USER_PERMISSIONS = 32
PLEX_API = "https://plex.tv/api/v2"


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
# The stored Plex-token mapping
# --------------------------------------------------------------------------- #


@respx.mock
async def test_actions_stores_the_mapping_that_search_and_grab_rely_on(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    assert await count_tokens(db) == 0

    await client.get("/actions", headers=plex_headers)

    assert await count_tokens(db) == 1
    user = await resolve_token(db, PLEX_TOKEN)
    assert user is not None
    assert user.seerr_user_id == 42


@respx.mock
async def test_the_raw_plex_token_is_never_stored(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Only a SHA-256 fingerprint is persisted, so the table cannot yield a
    # working Plex credential even if the database file leaks.
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)

    stored = (await db.execute(select(PlexTokenSession))).scalars().all()
    assert [row.token_fingerprint for row in stored] == [token_fingerprint(PLEX_TOKEN)]
    assert PLEX_TOKEN not in [row.token_fingerprint for row in stored]
    assert len(stored[0].token_fingerprint) == 64


@respx.mock
async def test_calling_actions_twice_refreshes_rather_than_duplicates(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)
    await client.get("/actions", headers=plex_headers)

    assert await count_tokens(db) == 1


@respx.mock
async def test_the_mapping_survives_a_restart(
    app: FastAPI, client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    """A restart used to 401 every client until its next launch."""
    mock_seerr_auth()
    respx.get(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    await client.get("/actions", headers=plex_headers)

    # Rebuild the whole app over the same database, which is what a restart is
    # as far as anything in memory is concerned.
    restarted = create_app(engine=app.state.cplus.engine, create_schema=False)
    async with LifespanManager(restarted):
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as fresh:
            response = await fresh.get(
                "/titles/tt0111161/actions", headers=plex_headers
            )

    assert response.status_code == 200


async def test_an_unknown_token_is_still_rejected(
    client: httpx.AsyncClient, configured: Config
) -> None:
    response = await client.get(
        "/titles/tt1/actions", headers={"X-Plex-Token": "never-seen"}
    )
    assert response.status_code == 401
    assert "GET /actions" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Reconnecting to a different Seerr instance invalidates cached identity
# --------------------------------------------------------------------------- #


@respx.mock
async def test_changing_the_seerr_url_forgets_every_cached_token(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)
    assert await count_tokens(db) == 1

    await apply_seerr_url_change(db, configured, "http://other-seerr.test:5055")
    await db.commit()

    assert await count_tokens(db) == 0


async def test_an_unchanged_seerr_url_is_a_no_op(
    db: AsyncSession, configured: Config
) -> None:
    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.flush()
    db.add(AdminSession(token="keep-me", user_id=admin.id))
    await db.commit()

    changed = await apply_seerr_url_change(db, configured, configured.seerr_url)

    assert changed is False
    assert (await db.execute(select(AdminSession))).scalars().first() is not None


async def test_changing_the_seerr_url_keeps_only_the_callers_own_session(
    db: AsyncSession, configured: Config
) -> None:
    mine = User(seerr_user_id=1, plex_username="owner")
    someone_else = User(seerr_user_id=2, plex_username="other-admin")
    db.add_all([mine, someone_else])
    await db.flush()
    db.add(AdminSession(token="mine", user_id=mine.id))
    db.add(AdminSession(token="someone-elses", user_id=someone_else.id))
    await db.commit()

    changed = await apply_seerr_url_change(
        db, configured, "http://other-seerr.test:5055", keep_session_token="mine"
    )
    await db.commit()

    assert changed is True
    tokens = {row.token for row in (await db.execute(select(AdminSession))).scalars().all()}
    assert tokens == {"mine"}


# --------------------------------------------------------------------------- #
# The webui sign-in (Plex PIN flow), and its admin gate
# --------------------------------------------------------------------------- #


async def pin_sign_in(
    client: httpx.AsyncClient,
    *,
    permissions: int,
    user_id: int = 1,
    pin_id: int = 100,
    seerr_url: str = SEERR_URL,
) -> httpx.Response:
    """Run the whole proxied PIN flow and return the final poll response."""
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": pin_id, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/{pin_id}").mock(
        return_value=httpx.Response(200, json={"authToken": PLEX_TOKEN})
    )
    mock_seerr_auth(user_id=user_id, permissions=permissions)

    await client.post("/admin/plex/pin", data={"seerr_url": seerr_url})
    return await client.get(f"/admin/plex/pin/{pin_id}")


@respx.mock
async def test_the_webui_signs_in_the_seerr_admin(
    client: httpx.AsyncClient, configured: Config
) -> None:
    response = await pin_sign_in(client, permissions=ADMIN_PERMISSIONS)

    assert response.status_code == 200
    assert response.json()["claimed"] is True
    assert SESSION_COOKIE_NAME in response.cookies


@respx.mock
async def test_the_webui_rejects_a_non_admin_with_a_clear_message(
    client: httpx.AsyncClient, configured: Config
) -> None:
    response = await pin_sign_in(client, permissions=PLAIN_USER_PERMISSIONS)

    assert response.status_code == 403
    assert "not the Seerr admin" in response.json()["detail"]
    assert SESSION_COOKIE_NAME not in response.cookies


@respx.mock
@pytest.mark.parametrize("permissions", [2, 2 | 32, 2 | 16 | 32])
async def test_the_admin_bit_is_checked_as_a_bitmask(
    client: httpx.AsyncClient, configured: Config, permissions: int
) -> None:
    response = await pin_sign_in(client, permissions=permissions, user_id=9)
    assert response.status_code == 200


@respx.mock
async def test_seerr_user_id_1_is_not_treated_as_admin_without_the_bit(
    client: httpx.AsyncClient, configured: Config
) -> None:
    # Admin-ness comes from the bitmask, never from being user 1.
    response = await pin_sign_in(client, permissions=PLAIN_USER_PERMISSIONS, user_id=1)
    assert response.status_code == 403


@respx.mock
async def test_first_run_bootstrap_persists_the_supplied_seerr_url(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # No config row at all: the admin cannot set the URL without a session, and
    # cannot get a session without the URL, so sign-in accepts it inline.
    response = await pin_sign_in(client, permissions=ADMIN_PERMISSIONS)

    assert response.status_code == 200
    config = (await db.execute(select(Config))).scalar_one()
    assert config.seerr_url == SEERR_URL


@respx.mock
async def test_a_seerr_url_that_fails_to_authenticate_is_not_persisted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 5, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/5").mock(
        return_value=httpx.Response(200, json={"authToken": PLEX_TOKEN})
    )
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )

    await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
    response = await client.get("/admin/plex/pin/5")

    assert response.status_code == 502
    config = (await db.execute(select(Config))).scalars().first()
    assert config is None or config.seerr_url is None


async def test_signing_in_without_any_seerr_url_is_400(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/admin/plex/pin", data={"seerr_url": ""})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# The two flows stay separate
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_webui_session_does_not_authenticate_the_tvos_endpoints(
    client: httpx.AsyncClient, configured: Config
) -> None:
    await pin_sign_in(client, permissions=ADMIN_PERMISSIONS)

    # The session cookie is set, but tvOS endpoints want a Plex token header.
    response = await client.get("/titles/tt0111161/actions")
    assert response.status_code == 401


@respx.mock
async def test_the_tvos_flow_issues_no_session_cookie(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    response = await client.get("/actions", headers=plex_headers)
    assert SESSION_COOKIE_NAME not in response.cookies


@respx.mock
async def test_a_plex_token_header_does_not_open_the_admin_ui(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/actions", headers=plex_headers)

    response = await client.get(
        "/admin/config", headers=plex_headers, follow_redirects=False
    )
    assert response.status_code == 303
