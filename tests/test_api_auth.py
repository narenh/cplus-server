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
from cplus_service.auth.identity import sync_seerr_instance
from cplus_service.auth.plex_cache import count_tokens, resolve_token, token_fingerprint
from cplus_service.auth.sessions import SESSION_COOKIE_NAME
from cplus_service.db.models import AdminSession, Config, PlexTokenSession, User
from cplus_service.settings import SEERR_URL_ENV

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
# GET /register — the tvOS checkpoint
# --------------------------------------------------------------------------- #


@respx.mock
async def test_register_validates_against_seerr_and_creates_the_user(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    route = mock_seerr_auth(user_id=42, username="alice")

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 200
    assert route.called
    assert route.calls[0].request.content == b'{"authToken":"plex-token-abc"}'

    user = (await db.execute(select(User))).scalar_one()
    assert user.seerr_user_id == 42
    assert user.plex_username == "alice"


@respx.mock
async def test_register_returns_no_action_details(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    # Actions only make sense in the context of a title, so this endpoint's one
    # job is validating the token and priming the cache — not describing what
    # the caller can do. GET /titles/{imdb_id}/actions owns that.
    mock_seerr_auth()
    await client.get("/register", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    stream_now = await make_action(db, "Stream Now")
    await grant(db, user, stream_now)

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 200
    assert "actions" not in response.json()


@respx.mock
async def test_register_upserts_rather_than_duplicating_on_every_launch(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(user_id=42, username="alice")
    await client.get("/register", headers=plex_headers)
    await client.get("/register", headers=plex_headers)

    users = (await db.execute(select(User))).scalars().all()
    assert len(users) == 1


@respx.mock
async def test_a_renamed_plex_account_refreshes_the_cached_username(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(user_id=42, username="alice")
    await client.get("/register", headers=plex_headers)

    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(user_id=42, username="alicia"))
    )
    await client.get("/register", headers=plex_headers)

    user = (await db.execute(select(User))).scalar_one()
    assert user.plex_username == "alicia"


async def test_register_without_a_plex_token_is_401(
    client: httpx.AsyncClient, configured: Config
) -> None:
    assert (await client.get("/register")).status_code == 401


@respx.mock
async def test_a_rejected_plex_token_is_401(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(403, json={"message": "Unauthorized"})
    )

    response = await client.get("/register", headers=plex_headers)
    assert response.status_code == 401


@respx.mock
async def test_an_unreachable_seerr_is_502_not_401(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    # A 401 here would make the client discard a perfectly good Plex token.
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )

    response = await client.get("/register", headers=plex_headers)
    assert response.status_code == 502


async def test_register_before_seerr_is_configured_is_503(
    client: httpx.AsyncClient, plex_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SEERR_URL_ENV, raising=False)

    response = await client.get("/register", headers=plex_headers)

    assert response.status_code == 503
    assert SEERR_URL_ENV in response.json()["detail"]


# --------------------------------------------------------------------------- #
# The stored Plex-token mapping
# --------------------------------------------------------------------------- #


@respx.mock
async def test_register_stores_the_mapping_that_titles_search_and_grab_rely_on(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    assert await count_tokens(db) == 0

    await client.get("/register", headers=plex_headers)

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
    await client.get("/register", headers=plex_headers)

    stored = (await db.execute(select(PlexTokenSession))).scalars().all()
    assert [row.token_fingerprint for row in stored] == [token_fingerprint(PLEX_TOKEN)]
    assert PLEX_TOKEN not in [row.token_fingerprint for row in stored]
    assert len(stored[0].token_fingerprint) == 64


@respx.mock
async def test_calling_register_twice_refreshes_rather_than_duplicates(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/register", headers=plex_headers)
    await client.get("/register", headers=plex_headers)

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
    await client.get("/register", headers=plex_headers)

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
    assert "GET /register" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Repointing the deployment at a different Seerr invalidates cached identity
# --------------------------------------------------------------------------- #


@respx.mock
async def test_repointing_at_another_seerr_forgets_every_cached_token(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_seerr_auth()
    await client.get("/register", headers=plex_headers)
    assert await count_tokens(db) == 1

    # The startup sync has already run once for this install, so the stored
    # fingerprint matches the environment; moving the environment is what a
    # redeploy against a different instance looks like.
    await sync_seerr_instance(db, configured)
    monkeypatch.setenv(SEERR_URL_ENV, "http://other-seerr.test:5055")

    assert await sync_seerr_instance(db, configured) is True
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

    # First call records the fingerprint; the second sees no change at all.
    await sync_seerr_instance(db, configured)
    changed = await sync_seerr_instance(db, configured)

    assert changed is False
    assert (await db.execute(select(AdminSession))).scalars().first() is not None


async def test_a_url_differing_only_in_trailing_slash_is_not_a_change(
    db: AsyncSession, configured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sync_seerr_instance(db, configured)

    monkeypatch.setenv(SEERR_URL_ENV, f"  {SEERR_URL}/  ")

    assert await sync_seerr_instance(db, configured) is False


async def test_repointing_signs_out_every_admin_including_the_last_one(
    db: AsyncSession, configured: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No carve-out: the flush happens at startup, when nobody holds a request."""
    mine = User(seerr_user_id=1, plex_username="owner")
    someone_else = User(seerr_user_id=2, plex_username="other-admin")
    db.add_all([mine, someone_else])
    await db.flush()
    db.add(AdminSession(token="mine", user_id=mine.id))
    db.add(AdminSession(token="someone-elses", user_id=someone_else.id))
    await db.commit()

    await sync_seerr_instance(db, configured)
    monkeypatch.setenv(SEERR_URL_ENV, "http://other-seerr.test:5055")

    assert await sync_seerr_instance(db, configured) is True
    await db.commit()

    assert (await db.execute(select(AdminSession))).scalars().all() == []


async def test_a_fresh_install_settles_after_its_first_start(
    db: AsyncSession, configured: Config
) -> None:
    """Startup records the fingerprint, so a boot that changed nothing flushes nothing.

    Without this the warning — and the sign-out — would fire on every restart.
    """
    assert configured.seerr_url_fingerprint is not None
    assert await sync_seerr_instance(db, configured) is False


# --------------------------------------------------------------------------- #
# The webui sign-in (Plex PIN flow), and its admin gate
# --------------------------------------------------------------------------- #


async def pin_sign_in(
    client: httpx.AsyncClient,
    *,
    permissions: int,
    user_id: int = 1,
    pin_id: int = 100,
) -> httpx.Response:
    """Run the whole proxied PIN flow and return the final poll response."""
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": pin_id, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/{pin_id}").mock(
        return_value=httpx.Response(200, json={"authToken": PLEX_TOKEN})
    )
    mock_seerr_auth(user_id=user_id, permissions=permissions)

    await client.post("/admin/plex/pin")
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
async def test_the_seerr_host_in_a_sign_in_request_is_ignored(
    client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The takeover this whole design exists to prevent.

    Both sign-in endpoints are unauthenticated — they have to be, they are how
    you authenticate. If a caller could name the Seerr instance, anyone could
    point a sign-in at a server of their own, have it answer "yes, admin", and
    be handed a session here. So a smuggled ``seerr_url`` must reach nothing:
    the token is validated against the environment's Seerr, which does not
    recognise the attacker, and no session is issued.
    """
    attacker_seerr = "http://attacker.test:5055"
    hostile = respx.post(f"{attacker_seerr}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(permissions=2))
    )
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 7, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/7").mock(
        return_value=httpx.Response(200, json={"authToken": PLEX_TOKEN})
    )
    # The real Seerr does not know this Plex account.
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    await client.post("/admin/plex/pin", data={"seerr_url": attacker_seerr})
    response = await client.get("/admin/plex/pin/7", params={"seerr_url": attacker_seerr})

    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in response.cookies
    assert not hostile.called
    assert (await db.execute(select(AdminSession))).scalars().all() == []


@respx.mock
async def test_signing_in_never_writes_a_seerr_url_to_the_database(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """Only a fingerprint is stored, and the URL itself has no column at all."""
    response = await pin_sign_in(client, permissions=ADMIN_PERMISSIONS)

    assert response.status_code == 200
    assert not hasattr(Config, "seerr_url")


async def test_signing_in_without_a_configured_seerr_is_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SEERR_URL_ENV, raising=False)

    response = await client.post("/admin/plex/pin")

    assert response.status_code == 503
    assert SEERR_URL_ENV in response.json()["detail"]


async def test_the_login_page_shows_the_configured_host_and_offers_no_field(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/admin/login")).text

    assert SEERR_URL in body
    # No input for it, and nothing that could post one.
    assert 'name="seerr_url"' not in body


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
    response = await client.get("/register", headers=plex_headers)
    assert SESSION_COOKIE_NAME not in response.cookies


@respx.mock
async def test_a_plex_token_header_does_not_open_the_admin_ui(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth()
    await client.get("/register", headers=plex_headers)

    response = await client.get(
        "/admin/config", headers=plex_headers, follow_redirects=False
    )
    assert response.status_code == 303
