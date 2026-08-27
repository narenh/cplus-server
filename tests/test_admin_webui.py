"""Admin webui tests.

These drive the real ASGI app with a real database and real templates — only
plex.tv, Seerr and Prowlarr are mocked. A template that fails to render, a route
that is not gated, or a rule that will not round-trip through stage 1's schema
all show up here.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.auth.plex_cache import remember_token
from cplus_service.auth.sessions import SESSION_COOKIE_NAME, create_session
from cplus_service.bootstrap import DEFAULT_PROFILE_NAME
from cplus_service.db.models import (
    Action,
    ActivityLog,
    AdminSession,
    Config,
    EventType,
    Grab,
    Permission,
    PlexTokenSession,
    QualityProfile,
    User,
)
from cplus_service.quality.models import QualityProfile as ProfileSchema
from cplus_service.settings import SEERR_URL_ENV

from .conftest import PROWLARR_URL, SEERR_URL, TMDB_BEARER_TOKEN, make_action

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


async def profile_named(db: AsyncSession, name: str) -> QualityProfile | None:
    """One profile by name.

    Tests name what they are looking for rather than assuming the table holds
    exactly one row: an install always starts with the seeded starter profile.
    """
    result = await db.execute(select(QualityProfile).where(QualityProfile.name == name))
    return result.scalars().first()


async def profile_names(db: AsyncSession) -> list[str]:
    result = await db.execute(select(QualityProfile.name).order_by(QualityProfile.name))
    return list(result.scalars().all())



def ticked_resolutions(html: str) -> list[str]:
    """The resolution boxes the choice rows come back with ticked, in row order."""
    return re.findall(
        r'name="choices-\d+-resolutions"\s+value="([^"]+)"\s+checked', html
    )


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
        "/admin/notifications",
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

    start = await client.post("/admin/plex/pin")
    assert start.status_code == 200
    assert start.json()["pin_id"] == 555
    assert "ABCD" in start.json()["auth_url"]

    claimed = await client.get("/admin/plex/pin/555")
    assert claimed.status_code == 200
    assert claimed.json()["claimed"] is True
    assert SESSION_COOKIE_NAME in claimed.cookies

    # Signing in writes no Seerr URL — there is nowhere to write one — but it
    # does mint the stable Plex client identifier on first use.
    config = (await db.execute(select(Config))).scalar_one()
    assert config.plex_client_identifier


@respx.mock
async def test_an_unclaimed_pin_is_not_an_error(
    client: httpx.AsyncClient, configured: Config
) -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 7, "code": "WXYZ"})
    )
    respx.get(f"{PLEX_API}/pins/7").mock(return_value=httpx.Response(200, json={}))

    await client.post("/admin/plex/pin")
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

    await client.post("/admin/plex/pin")
    response = await client.get("/admin/plex/pin/9")

    assert response.status_code == 403
    assert "not the Seerr admin" in response.json()["detail"]
    assert SESSION_COOKIE_NAME not in response.cookies


async def test_starting_a_pin_without_a_configured_seerr_is_rejected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SEERR_URL_ENV, raising=False)

    response = await client.post("/admin/plex/pin")

    assert response.status_code == 503
    assert SEERR_URL_ENV in response.json()["detail"]


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
        created_at=datetime.now(UTC) - PENDING_LOGIN_TTL - timedelta(minutes=1),
    )
    state.pending_plex_logins[222] = PendingPlexLogin(created_at=datetime.now(UTC))

    with respx.mock:
        respx.post(f"{PLEX_API}/pins").mock(
            return_value=httpx.Response(201, json={"id": 333, "code": "NEWW"})
        )
        response = await client.post("/admin/plex/pin")
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
            "prowlarr_url": f"{PROWLARR_URL}/",
            "prowlarr_api_key": "the-key",
            "preferred_indexer_id": "3",
            "tmdb_bearer_token": "the-tmdb-token",
        },
    )
    assert response.status_code == 200

    config = (await db.execute(select(Config))).scalar_one()
    assert config.prowlarr_url == PROWLARR_URL  # trailing slash normalised away
    assert config.prowlarr_api_key == "the-key"
    assert config.preferred_indexer_id == 3
    assert config.tmdb_bearer_token == "the-tmdb-token"


async def test_a_blank_api_key_and_token_leave_the_saved_ones_alone(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await client.post(
        "/admin/config",
        data={
            "prowlarr_url": PROWLARR_URL,
            "prowlarr_api_key": "",
            "preferred_indexer_id": "",
            "tmdb_bearer_token": "",
        },
    )

    await db.refresh(configured)
    assert configured.prowlarr_api_key == "prowlarr-key"
    assert configured.tmdb_bearer_token == TMDB_BEARER_TOKEN
    # Empty means "All indexers", which is null rather than a sentinel.
    assert configured.preferred_indexer_id is None


async def test_the_saved_api_key_and_tmdb_token_are_never_rendered_into_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/config")
    assert "prowlarr-key" not in response.text
    assert TMDB_BEARER_TOKEN not in response.text


async def test_the_config_page_shows_the_seerr_host_read_only(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Displayed from the environment, with nothing on the page that can set it."""
    await signed_in(client, db)

    response = await client.get("/admin/config")

    assert SEERR_URL in response.text
    assert SEERR_URL_ENV in response.text
    # Not even a disabled input: there is no endpoint behind it any more.
    assert 'name="seerr_url"' not in response.text


async def test_there_is_no_endpoint_that_changes_the_seerr_host(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The old ``POST /admin/config/seerr-url`` is gone, not merely gated.

    An admin session is the strongest credential this service issues, so the
    check that matters is that even holding one cannot repoint the install.
    """
    admin = await signed_in(client, db)
    await remember_token(db, "some-tvos-plex-token", admin)
    await db.commit()

    response = await client.post(
        "/admin/config/seerr-url",
        data={"seerr_url": "http://a-different-seerr.test:5055"},
    )

    assert response.status_code == 404
    assert (await db.execute(select(PlexTokenSession))).scalars().first() is not None


async def test_saving_config_ignores_a_smuggled_seerr_url(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """``POST /admin/config`` has no such parameter, so one sent anyway is inert."""
    admin = await signed_in(client, db)
    await remember_token(db, "some-tvos-plex-token", admin)
    await db.commit()
    fingerprint = configured.seerr_url_fingerprint

    response = await client.post(
        "/admin/config",
        data={
            "seerr_url": "http://a-different-seerr.test:5055",
            "prowlarr_url": PROWLARR_URL,
            "prowlarr_api_key": "",
            "preferred_indexer_id": "",
            "tmdb_bearer_token": "",
        },
    )
    assert response.status_code == 200

    await db.refresh(configured)
    assert configured.seerr_url_fingerprint == fingerprint
    assert (await db.execute(select(PlexTokenSession))).scalars().first() is not None
    assert (await db.execute(select(AdminSession))).scalars().first() is not None


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

    profile = await profile_named(db, "4K")
    assert profile is not None
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

    profile = await profile_named(db, "Ordered")
    assert profile is not None
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
    assert await profile_named(db, "Bad") is None


async def test_a_profile_needs_a_name(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/quality-profiles", data={"name": "  "})
    assert response.status_code == 422
    # Nothing was stored: the starter profile seeded at startup is all there is.
    assert await profile_names(db) == [DEFAULT_PROFILE_NAME]


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

    profile = await profile_named(db, "Off")
    assert profile is not None
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
        data={
            "op": "add",
            "kind": "preference",
            "rule_type_preference": "audio_match",
            "rules-0-type": "exclude_prerelease",
        },
    )
    assert response.status_code == 200
    assert 'name="rules-1-type" value="audio_match"' in response.text


async def test_each_section_adds_from_its_own_dropdown(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Both dropdowns are inside the one form and both are submitted, so the
    # button has to say which one it meant.
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/rows",
        data={
            "op": "add",
            "kind": "filter",
            "rule_type_filter": "size_cap_gb",
            "rule_type_preference": "audio_match",
        },
    )
    assert response.status_code == 200
    assert 'value="size_cap_gb"' in response.text
    assert 'name="rules-0-type" value="audio_match"' not in response.text


async def test_a_move_never_crosses_the_filter_boundary(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Filters have no order and tie-breakers do; sliding one list into the
    # other would look like a change and be none.
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/rows",
        data={
            "op": "down",
            "index": "0",
            "rules-0-type": "size_cap_gb",
            "rules-0-value": "25",
            "rules-1-type": "size",
            "rules-1-direction": "largest",
        },
    )

    order = [
        line.split('value="')[1].split('"')[0]
        for line in response.text.splitlines()
        if 'name="rules-' in line and "-type" in line
    ]
    assert order == ["size_cap_gb", "size"]


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
    assert await profile_named(db, "Spare") is None


# --------------------------------------------------------------------------- #
# Choices, and the preview
# --------------------------------------------------------------------------- #


#: The profile an admin could not build before choices existed: two wants with
#: different size rules, ranked one above the other.
FOUR_K_OR_BIG_HD = {
    "name": "4K or big HD",
    "rules-0-type": "exclude_prerelease",
    "rules-0-enabled": "on",
    "choices-0-present": "1",
    "choices-0-resolutions": "2160p",
    "choices-0-sources": "WEB-DL",
    "choices-0-tie_break": "biggest",
    "choices-1-present": "1",
    "choices-1-resolutions": "1080p",
    "choices-1-max_size_gb": "15",
    "choices-1-tie_break": "biggest",
}


async def test_choices_are_stored_in_the_order_they_were_submitted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles", data=FOUR_K_OR_BIG_HD, follow_redirects=False
    )
    assert response.status_code == 303

    profile = await profile_named(db, "4K or big HD")
    assert profile is not None
    assert [choice["match"]["resolutions"] for choice in profile.choices] == [
        ["2160p"],
        ["1080p"],
    ]
    assert profile.choices[0]["match"]["max_size_gb"] is None
    assert profile.choices[1]["match"]["max_size_gb"] == 15.0
    assert [choice["tie_break"] for choice in profile.choices] == ["biggest", "biggest"]

    # And it round-trips into the schema the engine consumes.
    reparsed = ProfileSchema(
        name=profile.name, rules=profile.rules, choices=profile.choices
    )
    assert len(reparsed.choices) == 2


async def test_a_choice_with_nothing_ticked_survives_a_round_trip(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # It submits no fields of its own, so without the hidden marker the server
    # would read the row as deleted — and an admin would watch a row they just
    # added disappear when they touched anything else.
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/rows",
        data={"op": "", "choices-0-present": "1", "choices-1-present": "1"},
    )
    assert response.status_code == 200
    assert 'name="choices-0-present"' in response.text
    assert 'name="choices-1-present"' in response.text


async def test_the_choice_builder_reorders_and_removes(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    form = {
        "target": "choices",
        "choices-0-present": "1",
        "choices-0-resolutions": "2160p",
        "choices-1-present": "1",
        "choices-1-resolutions": "1080p",
    }

    moved = await client.post(
        "/admin/quality-profiles/rows", data={**form, "op": "up", "index": "1"}
    )
    assert ticked_resolutions(moved.text) == ["1080p", "2160p"]

    removed = await client.post(
        "/admin/quality-profiles/rows", data={**form, "op": "remove", "index": "0"}
    )
    assert 'name="choices-1-present"' not in removed.text


async def test_the_profile_reads_back_in_plain_english(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The complaint this whole page answers: which rules rank, which filter,
    # and what does the combination actually do?
    await signed_in(client, db)
    response = await client.post("/admin/quality-profiles/preview", data=FOUR_K_OR_BIG_HD)

    assert response.status_code == 200
    assert "4K · WEB-DL, biggest file" in response.text
    assert "1080p · under 15 GB, biggest file" in response.text
    assert "Never grab" in response.text


async def test_the_reading_travels_with_the_preview_so_it_cannot_go_stale(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The builder only re-renders on add/remove/move. If the reading lived
    # there it would ignore the checkbox you just ticked, and a confident wrong
    # description is worse than none — so it rides on the preview instead,
    # which redraws on every edit, and swaps itself back in out of band.
    await signed_in(client, db)
    rows = await client.post("/admin/quality-profiles/rows", data=FOUR_K_OR_BIG_HD)
    preview = await client.post("/admin/quality-profiles/preview", data=FOUR_K_OR_BIG_HD)

    assert 'id="profile-reading"' not in rows.text
    assert 'id="profile-reading" hx-swap-oob="true"' in preview.text


async def test_a_catch_all_choice_above_another_is_flagged(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Every choice below it is dead, and nothing about the form says so.
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/preview",
        data={
            "choices-0-present": "1",
            "choices-1-present": "1",
            "choices-1-resolutions": "2160p",
        },
    )
    assert "nothing after it can ever apply" in response.text


async def test_the_preview_ranks_the_sample_releases_through_the_draft(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Against the unsaved form, and with nothing configured — the sample cast
    # needs no Prowlarr and no saved profile.
    await signed_in(client, db)
    response = await client.post("/admin/quality-profiles/preview", data=FOUR_K_OR_BIG_HD)

    assert response.status_code == 200
    assert "2160p.WEB-DL" in response.text
    assert "1st choice" in response.text
    assert "2nd choice" in response.text
    # Nothing was saved by previewing.
    assert await profile_named(db, "4K or big HD") is None


async def test_the_preview_names_the_filter_that_dropped_a_release(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/preview",
        data={"rules-0-type": "exclude_prerelease", "rules-0-enabled": "on"},
    )

    assert "dropped: pre-release" in response.text
    # The dropped release is still listed — a candidate silently missing is the
    # confusion the preview exists to end.
    assert "HDTS" in response.text


async def test_the_preview_survives_a_half_typed_rule(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # It redraws on every keystroke, so an invalid draft is an ordinary state
    # here rather than an error worth a status code.
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/preview",
        data={"rules-0-type": "hdr_match", "rules-0-values": "NOT_A_TAG"},
    )

    assert response.status_code == 200
    assert "not valid yet" in response.text


@respx.mock
async def test_the_preview_can_run_a_real_prowlarr_search(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "title": "Real.Film.2024.2160p.WEB-DL.DDP5.1.Atmos-FLUX",
                    "guid": "real-uhd",
                    "indexerId": 1,
                    "size": 25 * GB,
                }
            ],
        )
    )
    await signed_in(client, db)

    response = await client.post(
        "/admin/quality-profiles/preview",
        data={
            **FOUR_K_OR_BIG_HD,
            "preview_source": "prowlarr",
            "preview_query": "tt0111161",
        },
    )

    assert response.status_code == 200
    assert "Real.Film.2024.2160p" in response.text


async def test_a_live_preview_without_prowlarr_says_so_instead_of_failing(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Losing a half-built profile to a failed lookup would be a worse answer
    # than "that didn't work".
    await signed_in(client, db)
    response = await client.post(
        "/admin/quality-profiles/preview",
        data={"preview_source": "prowlarr", "preview_query": "anything"},
    )

    assert response.status_code == 200
    assert "Prowlarr is not configured yet" in response.text


async def test_the_profile_list_reads_profiles_back_rather_than_listing_rule_types(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await client.post("/admin/quality-profiles", data=FOUR_K_OR_BIG_HD)

    response = await client.get("/admin/quality-profiles")

    assert "4K · WEB-DL, biggest file" in response.text
    assert "exclude_prerelease" not in response.text


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


async def test_no_name_is_reserved_because_no_name_identifies_anything(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The built-in action is found by its is_system flag and told apart on the
    # wire by `kind`, so "Request" is a label like any other. Only the ordinary
    # uniqueness constraint applies — and it is the built-in action that
    # already holds this one.
    await signed_in(client, db)
    profile = QualityProfile(name="P", rules=[])
    db.add(profile)
    await db.commit()

    taken = await client.post(
        "/admin/actions",
        data={
            "name": "Request",
            "download_client_id": "5",
            "quality_profile_id": str(profile.id),
        },
    )
    assert taken.status_code == 409
    assert "already exists" in taken.json()["detail"]

    # Free the name on the built-in action, and it is available like any other.
    system = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await client.post(
        f"/admin/actions/{system.id}", data={"name": "Ask the household"}
    )
    freed = await client.post(
        "/admin/actions",
        data={
            "name": "Request",
            "download_client_id": "5",
            "quality_profile_id": str(profile.id),
        },
        follow_redirects=False,
    )
    assert freed.status_code == 303


async def test_the_built_in_action_can_be_renamed(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Nothing routes on it: the server finds this action by is_system and the
    # client reads `kind`. So the admin who wants their request button filed
    # under different words may have them.
    await signed_in(client, db)
    system = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()

    response = await client.post(
        f"/admin/actions/{system.id}",
        data={"name": "Ask the household", "display_title": "Ask for this"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    await db.refresh(system)
    assert system.name == "Ask the household"
    assert system.display_title == "Ask for this"
    assert system.is_system is True


async def test_the_built_in_action_takes_no_prowlarr_targets(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # It files a request in Seerr and never touches Prowlarr, so a download
    # client is not something it could use.
    await signed_in(client, db)
    system = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()

    response = await client.post(
        f"/admin/actions/{system.id}",
        data={"name": "Request", "download_client_id": "5", "quality_profile_id": "1"},
    )
    assert response.status_code == 400
    assert "never touches" in response.json()["detail"]

    await db.refresh(system)
    assert system.download_client_id is None


async def test_the_built_in_action_cannot_be_deleted(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    system = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()

    delete = await client.post(f"/admin/actions/{system.id}/delete")
    assert delete.status_code == 403

    db.expunge_all()
    assert await db.get(Action, system.id) is not None


async def test_an_ordinary_action_still_needs_its_prowlarr_targets(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The optional form fields exist for the built-in action's row, not as a
    # way to strip a Prowlarr action of the things /grab depends on.
    await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    response = await client.post(
        f"/admin/actions/{action.id}", data={"name": "Stream Now"}
    )
    assert response.status_code == 400

    await db.refresh(action)
    assert action.download_client_id == 5


@respx.mock
async def test_a_new_admin_can_create_an_action_without_building_a_profile_first(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    # The whole point of seeding the starter profile: the Actions page of a
    # fresh install is usable, not a dead end pointing at another page.
    respx.get(f"{PROWLARR_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "qBittorrent"}])
    )
    await signed_in(client, db)

    page = await client.get("/admin/actions")
    assert page.status_code == 200
    assert "Create a quality profile first" not in page.text
    assert DEFAULT_PROFILE_NAME in page.text

    starter = await profile_named(db, DEFAULT_PROFILE_NAME)
    assert starter is not None

    created = await client.post(
        "/admin/actions",
        data={
            "name": "Stream Now",
            "download_client_id": "5",
            "quality_profile_id": str(starter.id),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303


async def test_an_action_can_carry_button_copy_separate_from_its_name(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    profile = QualityProfile(name="P", rules=[])
    db.add(profile)
    await db.commit()

    response = await client.post(
        "/admin/actions",
        data={
            "name": "Add to library in HD",
            "display_title": "  Play Now  ",
            "download_client_id": "5",
            "quality_profile_id": str(profile.id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    action = (
        await db.execute(select(Action).where(Action.name == "Add to library in HD"))
    ).scalar_one()
    assert action.display_title == "Play Now"
    assert action.button_title == "Play Now"


async def test_a_blank_button_title_falls_back_to_the_name(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    action = await make_action(db, "Stream Now")
    action.display_title = "Play Now"
    await db.commit()

    await client.post(
        f"/admin/actions/{action.id}",
        data={
            "name": "Stream Now",
            "display_title": "   ",
            "download_client_id": "5",
            "quality_profile_id": str(action.quality_profile_id),
        },
        follow_redirects=False,
    )

    await db.refresh(action)
    assert action.display_title is None
    assert action.button_title == "Stream Now"


async def test_a_button_title_longer_than_the_column_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    action = await make_action(db, "Stream Now")

    response = await client.post(
        f"/admin/actions/{action.id}",
        data={
            "name": "Stream Now",
            "display_title": "x" * 129,
            "download_client_id": "5",
            "quality_profile_id": str(action.quality_profile_id),
        },
    )
    assert response.status_code == 400

    await db.refresh(action)
    assert action.display_title is None


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

    await client.post("/admin/plex/pin")
    response = await client.get("/admin/plex/pin/32")

    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
