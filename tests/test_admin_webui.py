"""Admin webui tests.

These drive the real ASGI app with a real database and real templates — only
plex.tv, Seerr and Prowlarr are mocked. A template that fails to render, a route
that is not gated, or a rule that will not round-trip through stage 1's schema
all show up here.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.auth.sessions import SESSION_COOKIE_NAME, create_session
from cplus_service.db.models import (
    Action,
    ActivityLog,
    Config,
    EventType,
    Grab,
    Permission,
    QualityProfile,
    User,
)
from cplus_service.quality.models import QualityProfile as ProfileSchema

from .conftest import PROWLARR_URL, SEERR_URL, make_action

GB = 1024**3
PLEX_API = "https://plex.tv/api/v2"


async def signed_in(client: httpx.AsyncClient, db: AsyncSession) -> User:
    """Create an admin user and attach a real session cookie to the client."""
    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.flush()
    token = await create_session(db, admin.id)
    await db.commit()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return admin


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/admin/config",
        "/admin/quality-profiles",
        "/admin/quality-profiles/new",
        "/admin/actions",
        "/admin/users",
        "/admin/grabs",
        "/admin/activity-log",
        "/admin/prowlarr/indexers",
        "/admin/prowlarr/download-clients",
    ],
)
async def test_every_admin_page_redirects_when_signed_out(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_an_htmx_request_gets_a_redirect_header_not_a_page(
    client: httpx.AsyncClient,
) -> None:
    # A bare 303 would be followed transparently and the login page swapped
    # into whatever fragment triggered it.
    response = await client.get(
        "/admin/prowlarr/indexers", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/admin/login"


async def test_an_expired_session_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    from datetime import UTC, datetime, timedelta

    from cplus_service.auth.sessions import SESSION_TTL
    from cplus_service.db.models import AdminSession

    admin = await signed_in(client, db)
    record = (await db.execute(select(AdminSession))).scalar_one()
    record.created_at = datetime.now(UTC) - SESSION_TTL - timedelta(minutes=1)
    await db.commit()

    response = await client.get("/admin/config", follow_redirects=False)
    assert response.status_code == 303
    assert admin.id is not None

    # The row is not deleted on the rejection path — raising the redirect rolls
    # the request transaction back — so the startup sweep is what removes it.
    from cplus_service.auth.sessions import purge_expired_sessions

    assert await purge_expired_sessions(db) == 1
    await db.commit()
    assert (await db.execute(select(AdminSession))).scalars().first() is None


# --------------------------------------------------------------------------- #
# Login / PIN flow
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_pin_flow_signs_in_a_seerr_admin(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 555, "code": "ABCD"})
    )
    respx.get(f"{PLEX_API}/pins/555").mock(
        return_value=httpx.Response(200, json={"authToken": "plex-tok"})
    )
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "permissions": 2, "plexUsername": "owner"}
        )
    )

    start = await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
    assert start.status_code == 200
    assert start.json()["pin_id"] == 555
    assert "ABCD" in start.json()["auth_url"]

    claimed = await client.get("/admin/plex/pin/555")
    assert claimed.status_code == 200
    assert claimed.json()["claimed"] is True
    assert SESSION_COOKIE_NAME in claimed.cookies

    # The Seerr URL is persisted only now that it has proven it authenticates.
    config = (await db.execute(select(Config))).scalar_one()
    assert config.seerr_url == SEERR_URL
    assert config.plex_client_identifier


@respx.mock
async def test_an_unclaimed_pin_is_not_an_error(
    client: httpx.AsyncClient, configured: Config
) -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 7, "code": "WXYZ"})
    )
    respx.get(f"{PLEX_API}/pins/7").mock(return_value=httpx.Response(200, json={}))

    await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
    response = await client.get("/admin/plex/pin/7")

    assert response.status_code == 200
    assert response.json() == {"claimed": False}
    assert SESSION_COOKIE_NAME not in response.cookies


@respx.mock
async def test_a_non_admin_cannot_sign_into_the_webui(
    client: httpx.AsyncClient, configured: Config
) -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 9, "code": "PQRS"})
    )
    respx.get(f"{PLEX_API}/pins/9").mock(
        return_value=httpx.Response(200, json={"authToken": "plex-tok"})
    )
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json={"id": 5, "permissions": 32, "plexUsername": "regular"}
        )
    )

    await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
    response = await client.get("/admin/plex/pin/9")

    assert response.status_code == 403
    assert "not the Seerr admin" in response.json()["detail"]
    assert SESSION_COOKIE_NAME not in response.cookies


async def test_starting_a_pin_without_a_seerr_url_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/admin/plex/pin", data={"seerr_url": ""})
    assert response.status_code == 400


async def test_polling_an_unknown_pin_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/plex/pin/12345")).status_code == 404


async def test_a_pending_login_past_its_ttl_reads_as_expired(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    # POST /admin/plex/pin takes no auth, so an abandoned or repeatedly
    # triggered sign-in must not sit in state.pending_plex_logins forever.
    from datetime import UTC, datetime, timedelta

    from cplus_service.api.routes.admin.login import PENDING_LOGIN_TTL
    from cplus_service.api.state import PendingPlexLogin

    state = app.state.cplus
    state.pending_plex_logins[999] = PendingPlexLogin(
        seerr_url=SEERR_URL,
        created_at=datetime.now(UTC) - PENDING_LOGIN_TTL - timedelta(minutes=1),
    )

    response = await client.get("/admin/plex/pin/999")

    assert response.status_code == 404
    assert "expired" in response.json()["detail"]
    # Swept, not merely ignored.
    assert 999 not in state.pending_plex_logins


async def test_starting_a_pin_sweeps_other_expired_pending_logins(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    from datetime import UTC, datetime, timedelta

    from cplus_service.api.routes.admin.login import PENDING_LOGIN_TTL
    from cplus_service.api.state import PendingPlexLogin

    state = app.state.cplus
    state.pending_plex_logins[111] = PendingPlexLogin(
        seerr_url=SEERR_URL,
        created_at=datetime.now(UTC) - PENDING_LOGIN_TTL - timedelta(minutes=1),
    )
    state.pending_plex_logins[222] = PendingPlexLogin(
        seerr_url=SEERR_URL, created_at=datetime.now(UTC)
    )

    with respx.mock:
        respx.post(f"{PLEX_API}/pins").mock(
            return_value=httpx.Response(201, json={"id": 333, "code": "NEWW"})
        )
        response = await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
        assert response.status_code == 200

    # The expired one is gone; the fresh one and the new one are untouched.
    assert 111 not in state.pending_plex_logins
    assert 222 in state.pending_plex_logins
    assert 333 in state.pending_plex_logins


async def test_logout_clears_the_cookie(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/logout", follow_redirects=False)
    assert response.status_code == 303
    assert (await client.get("/admin/config", follow_redirects=False)).status_code == 303


# --------------------------------------------------------------------------- #
# Config page
# --------------------------------------------------------------------------- #


async def test_config_page_renders(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/config")
    assert response.status_code == 200
    assert "Prowlarr" in response.text


async def test_saving_config_stores_the_values(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/config",
        data={
            "seerr_url": f"{SEERR_URL}/",
            "prowlarr_url": f"{PROWLARR_URL}/",
            "prowlarr_api_key": "the-key",
            "preferred_indexer_id": "3",
        },
    )
    assert response.status_code == 200

    config = (await db.execute(select(Config))).scalar_one()
    assert config.seerr_url == SEERR_URL  # trailing slash normalised away
    assert config.prowlarr_api_key == "the-key"
    assert config.preferred_indexer_id == 3


async def test_a_blank_api_key_leaves_the_saved_one_alone(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await client.post(
        "/admin/config",
        data={
            "seerr_url": SEERR_URL,
            "prowlarr_url": PROWLARR_URL,
            "prowlarr_api_key": "",
            "preferred_indexer_id": "",
        },
    )

    await db.refresh(configured)
    assert configured.prowlarr_api_key == "prowlarr-key"
    # Empty means "All indexers", which is null rather than a sentinel.
    assert configured.preferred_indexer_id is None


async def test_the_saved_api_key_is_never_rendered_into_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/config")
    assert "prowlarr-key" not in response.text


@respx.mock
async def test_verify_prowlarr_reports_success(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Prowlarr", "version": "1.28.0"})
    )
    await signed_in(client, db)

    as_json = await client.post("/admin/config/verify-prowlarr")
    assert as_json.json()["ok"] is True
    assert "1.28.0" in as_json.json()["message"]

    as_html = await client.post("/admin/config/verify-prowlarr?format=html")
    assert "1.28.0" in as_html.text


@respx.mock
async def test_verify_prowlarr_reports_a_bad_key(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    await signed_in(client, db)

    response = await client.post("/admin/config/verify-prowlarr")
    assert response.json()["ok"] is False
    assert "401" in response.json()["message"]


async def test_verify_prowlarr_before_configuring_is_not_a_crash(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/config/verify-prowlarr")
    assert response.status_code == 200
    assert response.json()["ok"] is False


@respx.mock
async def test_indexer_options_offer_all_indexers_plus_prowlarrs(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/indexer").mock(
        return_value=httpx.Response(
            200, json=[{"id": 3, "name": "Tracker One", "enable": True}]
        )
    )
    await signed_in(client, db)

    as_json = await client.get("/admin/prowlarr/indexers")
    assert as_json.json()["indexers"][0]["name"] == "Tracker One"

    as_html = await client.get("/admin/prowlarr/indexers?format=html")
    assert "All indexers" in as_html.text
    assert 'value="3"' in as_html.text


@respx.mock
async def test_download_client_options(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "qBittorrent"}])
    )
    await signed_in(client, db)

    assert (await client.get("/admin/prowlarr/download-clients")).json()[
        "download_clients"
    ][0]["name"] == "qBittorrent"
    assert "qBittorrent" in (
        await client.get("/admin/prowlarr/download-clients?format=html")
    ).text


# --------------------------------------------------------------------------- #
# Quality profiles and the rule builder
# --------------------------------------------------------------------------- #


async def test_creating_a_profile_stores_rules_the_engine_accepts(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/quality-profiles",
        data={
            "name": "4K",
            "rules-0-type": "exclude_prerelease",
            "rules-0-enabled": "on",
            "rules-1-type": "resolution_order",
            "rules-1-values": "2160p, 1080p",
            "rules-2-type": "size",
            "rules-2-direction": "largest",
            "rules-2-cap_gb": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    profile = (await db.execute(select(QualityProfile))).scalar_one()
    assert profile.name == "4K"
    assert [rule["type"] for rule in profile.rules] == [
        "exclude_prerelease",
        "resolution_order",
        "size",
    ]
    # Round-trips into stage 1's schema, so the engine can consume it as stored.
    reparsed = ProfileSchema(name=profile.name, rules=profile.rules)
    assert reparsed.rules[1].values == ["2160p", "1080p"]


async def test_rule_order_is_preserved_exactly_as_submitted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await client.post(
        "/admin/quality-profiles",
        data={
            "name": "Ordered",
            "rules-0-type": "source_order",
            "rules-0-values": "REMUX, BluRay",
            "rules-1-type": "resolution_order",
            "rules-1-values": "1080p, 2160p",
        },
        follow_redirects=False,
    )

    profile = (await db.execute(select(QualityProfile))).scalar_one()
    assert [rule["type"] for rule in profile.rules] == ["source_order", "resolution_order"]
    assert profile.rules[0]["values"] == ["REMUX", "BluRay"]
    assert profile.rules[1]["values"] == ["1080p", "2160p"]


async def test_an_invalid_rule_value_is_reported_not_stored(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles",
        data={
            "name": "Bad",
            "rules-0-type": "hdr_match",
            "rules-0-values": "DV_P8, NOT_A_REAL_TAG",
        },
    )

    assert response.status_code == 422
    assert "NOT_A_REAL_TAG" in response.text
    assert (await db.execute(select(QualityProfile))).scalars().first() is None


async def test_a_profile_needs_a_name(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/quality-profiles", data={"name": "  "})
    assert response.status_code == 422
    assert (await db.execute(select(QualityProfile))).scalars().first() is None


async def test_editing_a_profile_replaces_its_rules(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    profile = QualityProfile(
        name="Old", rules=[{"type": "repack_proper_priority", "enabled": True}]
    )
    db.add(profile)
    await db.commit()

    await client.post(
        "/admin/quality-profiles",
        data={
            "profile_id": str(profile.id),
            "name": "New",
            "rules-0-type": "size_cap_gb",
            "rules-0-value": "25",
        },
        follow_redirects=False,
    )

    await db.refresh(profile)
    assert profile.name == "New"
    assert profile.rules == [{"type": "size_cap_gb", "value": 25.0}]


async def test_an_unchecked_toggle_is_stored_as_disabled(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # An unchecked checkbox submits nothing at all, which must not read as "on".
    await signed_in(client, db)
    await client.post(
        "/admin/quality-profiles",
        data={"name": "Off", "rules-0-type": "exclude_prerelease"},
        follow_redirects=False,
    )

    profile = (await db.execute(select(QualityProfile))).scalar_one()
    assert profile.rules[0]["enabled"] is False


@pytest.mark.parametrize(
    ("op", "index", "expected"),
    [
        ("up", 1, ["size_cap_gb", "exclude_prerelease", "size"]),
        ("down", 0, ["size_cap_gb", "exclude_prerelease", "size"]),
        ("remove", 1, ["exclude_prerelease", "size"]),
        # Out-of-range indices redraw the list rather than erroring.
        ("up", 0, ["exclude_prerelease", "size_cap_gb", "size"]),
        ("down", 2, ["exclude_prerelease", "size_cap_gb", "size"]),
        ("remove", 99, ["exclude_prerelease", "size_cap_gb", "size"]),
    ],
)
async def test_the_rule_builder_reorders_without_server_side_draft_state(
    client: httpx.AsyncClient, db: AsyncSession, op: str, index: int, expected: list[str]
) -> None:
    await signed_in(client, db)
    form = {
        "op": op,
        "index": str(index),
        "rules-0-type": "exclude_prerelease",
        "rules-1-type": "size_cap_gb",
        "rules-1-value": "25",
        "rules-2-type": "size",
        "rules-2-direction": "largest",
    }

    response = await client.post("/admin/quality-profiles/rows", data=form)
    assert response.status_code == 200

    order = [
        line.split('value="')[1].split('"')[0]
        for line in response.text.splitlines()
        if 'name="rules-' in line and "-type" in line
    ]
    assert order == expected


async def test_the_rule_builder_can_add_a_rule(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/rows",
        data={"op": "add", "rule_type": "audio_match", "rules-0-type": "exclude_prerelease"},
    )
    assert response.status_code == 200
    assert 'name="rules-1-type" value="audio_match"' in response.text


async def test_a_profile_in_use_cannot_be_deleted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    response = await client.post(
        f"/admin/quality-profiles/{action.quality_profile_id}/delete"
    )
    assert response.status_code == 409
    assert "Stream Now" in response.json()["detail"]


async def test_an_unused_profile_can_be_deleted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    profile = QualityProfile(name="Spare", rules=[])
    db.add(profile)
    await db.commit()

    response = await client.post(
        f"/admin/quality-profiles/{profile.id}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    assert (await db.execute(select(QualityProfile))).scalars().first() is None


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_actions_page_lists_request_as_read_only(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "qBittorrent"}])
    )
    await signed_in(client, db)
    await make_action(db, "Stream Now")

    response = await client.get("/admin/actions")
    assert response.status_code == 200
    assert "Request" in response.text
    assert "built in" in response.text
    assert "grant per user" in response.text


async def test_creating_an_action(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)
    profile = QualityProfile(name="P", rules=[])
    db.add(profile)
    await db.commit()

    response = await client.post(
        "/admin/actions",
        data={"name": "Add 4K", "download_client_id": "5", "quality_profile_id": str(profile.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    action = (
        await db.execute(select(Action).where(Action.name == "Add 4K"))
    ).scalar_one()
    assert action.download_client_id == 5
    assert action.is_system is False


async def test_an_action_cannot_take_the_reserved_request_name(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The tvOS client routes on this name; letting an admin reuse it would
    # silently break every client.
    await signed_in(client, db)
    profile = QualityProfile(name="P", rules=[])
    db.add(profile)
    await db.commit()

    for name in ("Request", "request", "REQUEST"):
        response = await client.post(
            "/admin/actions",
            data={
                "name": name,
                "download_client_id": "5",
                "quality_profile_id": str(profile.id),
            },
        )
        assert response.status_code == 409, name


async def test_the_built_in_action_cannot_be_edited_or_deleted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    system = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()

    edit = await client.post(
        f"/admin/actions/{system.id}",
        data={"name": "Renamed", "download_client_id": "5", "quality_profile_id": "1"},
    )
    assert edit.status_code == 403

    delete = await client.post(f"/admin/actions/{system.id}/delete")
    assert delete.status_code == 403

    await db.refresh(system)
    assert system.name == "Request"


async def test_editing_and_deleting_an_ordinary_action(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    await client.post(
        f"/admin/actions/{action.id}",
        data={
            "name": "Stream Later",
            "download_client_id": "9",
            "quality_profile_id": str(action.quality_profile_id),
        },
        follow_redirects=False,
    )
    await db.refresh(action)
    assert action.name == "Stream Later"
    assert action.download_client_id == 9

    await client.post(f"/admin/actions/{action.id}/delete", follow_redirects=False)
    db.expunge_all()
    assert await db.get(Action, action.id) is None


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #


async def test_permissions_page_lists_users_and_actions(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    admin = await signed_in(client, db)
    await make_action(db, "Stream Now")

    response = await client.get("/admin/users")
    assert response.status_code == 200
    assert admin.plex_username in response.text
    assert "Stream Now" in response.text
    assert "Request" in response.text


async def test_toggling_a_permission_on_and_off(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    granted = await client.post(
        f"/admin/users/{user.id}/permissions",
        data={"action_id": str(action.id), "granted": "on"},
    )
    assert granted.status_code == 200
    assert "checked" in granted.text
    assert (await db.execute(select(Permission))).scalars().first() is not None

    revoked = await client.post(
        f"/admin/users/{user.id}/permissions", data={"action_id": str(action.id)}
    )
    assert "checked" not in revoked.text
    assert (await db.execute(select(Permission))).scalars().first() is None


async def test_granting_twice_is_idempotent(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    for _ in range(2):
        response = await client.post(
            f"/admin/users/{user.id}/permissions",
            data={"action_id": str(action.id), "granted": "on"},
        )
        assert response.status_code == 200

    assert len((await db.execute(select(Permission))).scalars().all()) == 1


async def test_removing_a_user_revokes_stored_access_immediately(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    from cplus_service.auth.plex_cache import count_tokens, remember_token

    user = await signed_in(client, db)
    await remember_token(db, "their-token", user)
    await db.commit()
    assert await count_tokens(db) == 1

    await client.post(f"/admin/users/{user.id}/delete", follow_redirects=False)

    # The FK cascade takes the stored Plex-token mapping with the user, so their
    # access ends at once rather than at their next launch.
    db.expunge_all()
    assert await count_tokens(db) == 0
    assert await db.get(User, user.id) is None


# --------------------------------------------------------------------------- #
# Grabs and activity log
# --------------------------------------------------------------------------- #


async def test_grabs_page_lists_and_filters(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    admin = await signed_in(client, db)
    other = User(seerr_user_id=2, plex_username="someone")
    db.add(other)
    await db.flush()
    action = await make_action(db, "Stream Now")

    db.add_all(
        [
            Grab(
                user_id=admin.id,
                action_id=action.id,
                release_title="Movie.2024.2160p.WEB-DL-FLUX",
                release_guid="g1",
                indexer_id=1,
                size_bytes=25 * GB,
            ),
            Grab(
                user_id=other.id,
                action_id=action.id,
                release_title="Other.2024.1080p.WEB-DL-GRP",
                release_guid="g2",
                indexer_id=2,
                size_bytes=8 * GB,
            ),
        ]
    )
    await db.commit()

    everyone = await client.get("/admin/grabs")
    assert "Movie.2024.2160p.WEB-DL-FLUX" in everyone.text
    assert "Other.2024.1080p.WEB-DL-GRP" in everyone.text
    assert "25.00 GB" in everyone.text

    filtered = await client.get("/admin/grabs", params={"user_id": other.id})
    assert "Movie.2024.2160p.WEB-DL-FLUX" not in filtered.text
    assert "Other.2024.1080p.WEB-DL-GRP" in filtered.text


async def test_activity_log_renders_searches_grabs_and_requests(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    admin = await signed_in(client, db)
    db.add_all(
        [
            ActivityLog(
                user_id=admin.id,
                event_type=EventType.SEARCH,
                detail={"imdb_id": "tt0111161", "action_ids": [1, 2]},
            ),
            ActivityLog(
                user_id=admin.id,
                event_type=EventType.GRAB,
                detail={"kind": "request", "tmdb_id": 1399, "type": "tv", "seasons": [1, 2]},
            ),
            ActivityLog(
                user_id=admin.id,
                event_type=EventType.GRAB,
                detail={"release_title": "Movie.2024-GRP", "success": False, "error": "boom"},
            ),
        ]
    )
    await db.commit()

    response = await client.get("/admin/activity-log")
    assert response.status_code == 200
    assert "tt0111161" in response.text
    assert "tmdb 1399" in response.text
    assert "boom" in response.text
    assert "request" in response.text


async def test_the_root_path_goes_to_the_admin_ui(client: httpx.AsyncClient) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/admin/config"


# --------------------------------------------------------------------------- #
# Behind a TLS-terminating proxy
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_session_cookie_is_secure_over_https(app: FastAPI) -> None:
    # Coolify terminates TLS and forwards plain HTTP, so the Secure flag is
    # decided from the forwarded scheme rather than from a setting.
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 31, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/31").mock(
        return_value=httpx.Response(200, json={"authToken": "tok"})
    )
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "permissions": 2, "plexUsername": "owner"}
        )
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as tls:
        await tls.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
        response = await tls.get("/admin/plex/pin/31")

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


@respx.mock
async def test_the_session_cookie_is_not_secure_over_plain_http(
    client: httpx.AsyncClient, configured: Config
) -> None:
    # Otherwise local development over http:// could never stay signed in.
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 32, "code": "CODE"})
    )
    respx.get(f"{PLEX_API}/pins/32").mock(
        return_value=httpx.Response(200, json={"authToken": "tok"})
    )
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "permissions": 2, "plexUsername": "owner"}
        )
    )

    await client.post("/admin/plex/pin", data={"seerr_url": SEERR_URL})
    response = await client.get("/admin/plex/pin/32")

    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
