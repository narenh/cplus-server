"""Device registration, and the two paths that actually raise a notification.

End-to-end through the real app: a grab or a request goes in, and a push comes
out at Apple's mocked door with the right two lines on it. These are the tests
that would catch an emitter being dropped in a refactor, which the unit tests
above them cannot see.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Action, ApnsDevice, Config, User

from .conftest import (
    APNS_PRODUCTION,
    APNS_SANDBOX,
    PROWLARR_URL,
    SEERR_URL,
    configure_apns,
    grant,
    make_action,
    register_device,
    seerr_user_payload,
)

GB = 1024**3
ADMIN_PERMISSIONS = 2
MANAGE_REQUESTS_PERMISSIONS = 16
DEVICE_TOKEN = "ab" * 32
OTHER_TOKEN = "cd" * 32


def mock_seerr_auth(**kwargs) -> respx.Route:  # noqa: ANN003
    return respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload(**kwargs))
    )


def mock_apns(host: str = APNS_PRODUCTION) -> respx.Route:
    return respx.post(url__startswith=host).mock(return_value=httpx.Response(200))


async def authenticate(client: httpx.AsyncClient, headers: dict) -> None:
    assert (await client.get("/register", headers=headers)).status_code == 200


def alert_of(route: respx.Route) -> dict:
    """The ``aps.alert`` of the last push Apple was sent."""
    return json.loads(route.calls.last.request.content)["aps"]["alert"]


# --------------------------------------------------------------------------- #
# POST /manager/push-devices
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_admin_can_register_a_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)

    response = await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={
            "device_token": DEVICE_TOKEN,
            "environment": "sandbox",
            "device_name": "Naren's Apple TV",
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is True

    device = await db.get(ApnsDevice, DEVICE_TOKEN)
    assert device is not None
    assert device.environment == "sandbox"
    assert device.device_name == "Naren's Apple TV"


@respx.mock
async def test_registering_again_updates_rather_than_duplicates(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """The app calls this on every launch; Apple hands back the same token."""
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)

    for name in ("First name", "Renamed device"):
        response = await client.post(
            "/manager/push-devices",
            headers=plex_headers,
            json={"device_token": DEVICE_TOKEN, "device_name": name},
        )
        assert response.status_code == 200

    devices = (await db.execute(select(ApnsDevice))).scalars().all()
    assert len(devices) == 1
    assert devices[0].device_name == "Renamed device"


@respx.mock
async def test_a_device_moves_to_whoever_signed_in_on_it(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Otherwise the previous owner keeps being notified through hardware they lost."""
    mock_seerr_auth(user_id=1, username="first", permissions=ADMIN_PERMISSIONS)
    await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    mock_seerr_auth(user_id=2, username="second", permissions=ADMIN_PERMISSIONS)
    await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    devices = (await db.execute(select(ApnsDevice))).scalars().all()
    assert len(devices) == 1
    owner = await db.get(User, devices[0].user_id)
    assert owner is not None and owner.plex_username == "second"


@respx.mock
async def test_a_non_admin_cannot_register_for_notifications(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """The ADMIN gate here is the whole of the access control on notifications."""
    # Can manage requests, which is enough for the rest of /manager/*, but is
    # not an admin — and notifications gate on ADMIN.
    mock_seerr_auth(permissions=MANAGE_REQUESTS_PERMISSIONS)

    response = await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    assert response.status_code == 403
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


async def test_registering_without_a_plex_token_is_401(
    client: httpx.AsyncClient, configured: Config
) -> None:
    response = await client.post(
        "/manager/push-devices", json={"device_token": DEVICE_TOKEN}
    )
    assert response.status_code == 401


@respx.mock
async def test_a_device_token_that_is_not_hex_is_rejected(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)

    response = await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": "not-a-device-token"},
    )

    assert response.status_code == 422


@respx.mock
async def test_an_app_can_unregister_its_own_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)
    await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    response = await client.delete(
        f"/manager/push-devices/{DEVICE_TOKEN}", headers=plex_headers
    )

    assert response.status_code == 200
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


@respx.mock
async def test_unregistering_a_device_that_is_not_registered_still_succeeds(
    client: httpx.AsyncClient, configured: Config, plex_headers: dict
) -> None:
    """The caller asked for it to be gone and it is gone."""
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)

    response = await client.delete(
        f"/manager/push-devices/{DEVICE_TOKEN}", headers=plex_headers
    )
    assert response.status_code == 200


@respx.mock
async def test_one_admin_cannot_unregister_anothers_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Sign-out is for your own device; the admin console is where the rest lives."""
    mock_seerr_auth(user_id=1, username="first", permissions=ADMIN_PERMISSIONS)
    await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    mock_seerr_auth(user_id=2, username="second", permissions=ADMIN_PERMISSIONS)
    response = await client.delete(
        f"/manager/push-devices/{DEVICE_TOKEN}", headers=plex_headers
    )

    assert response.status_code == 200
    assert await db.get(ApnsDevice, DEVICE_TOKEN) is not None


# --------------------------------------------------------------------------- #
# Arranging an install that can actually notify
# --------------------------------------------------------------------------- #


async def an_admin_with_a_device(
    db: AsyncSession,
    configured: Config,
    key_pem: str,
    *,
    device_token: str = DEVICE_TOKEN,
) -> User:
    """An admin holding a registered device, on a fully configured install."""
    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.commit()
    await register_device(db, admin, device_token=device_token)
    await configure_apns(db, configured, key_pem)
    return admin


async def the_calling_user(db: AsyncSession, username: str) -> User:
    """The user ``/register`` created for the caller's Plex token."""
    return (
        await db.execute(select(User).where(User.plex_username == username))
    ).scalar_one()


async def grant_request_action(db: AsyncSession, user: User) -> None:
    action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, user, action)


# --------------------------------------------------------------------------- #
# POST /request raises a notification
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_request_notifies_the_admin(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """The whole point, end to end: someone requests, the admin's phone says so."""
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99, "status": 1})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Jane Dietrich"))

    response = await client.post(
        "/request",
        headers=plex_headers,
        json={
            "tmdb_id": 603,
            "type": "movie",
            "media_title": "The End of Oak Street",
            "media_year": 2026,
        },
    )

    assert response.status_code == 200
    assert apns.called
    assert alert_of(apns) == {
        "title": "The End of Oak Street (2026)",
        "subtitle": "Requested by Jane Dietrich",
    }


@respx.mock
async def test_a_request_without_a_title_falls_back_to_the_tmdb_id(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """Ugly, and better than either a TMDB round trip or the word 'Unknown'."""
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Jane Dietrich"))

    await client.post(
        "/request", headers=plex_headers, json={"tmdb_id": 603, "type": "movie"}
    )

    assert alert_of(apns)["title"] == "TMDB 603"


@respx.mock
async def test_a_request_seerr_rejects_notifies_nobody(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """Nothing happened, so there is nothing to be told about."""
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(409, json={"message": "Already requested"})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Jane Dietrich"))

    response = await client.post(
        "/request", headers=plex_headers, json={"tmdb_id": 603, "type": "movie"}
    )

    assert response.status_code == 409
    assert not apns.called


# --------------------------------------------------------------------------- #
# POST /grab raises a notification; POST /manager/grab does not
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_action_grab_notifies_the_admin(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Jane Dietrich"), action)

    response = await client.post(
        "/grab",
        headers=plex_headers,
        json={
            "action_id": action.id,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
            "media_title": "I Love Boosters",
            "media_year": 2026,
        },
    )

    assert response.status_code == 200
    assert alert_of(apns) == {
        "title": "I Love Boosters (2026)",
        "subtitle": "Jane Dietrich: Stream Now",
    }


@respx.mock
async def test_a_grab_without_a_title_falls_back_to_the_release_name(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """An older client that has not learned to send media_title still reads well."""
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Jane Dietrich"), action)

    await client.post(
        "/grab",
        headers=plex_headers,
        json={
            "action_id": action.id,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
        },
    )

    assert alert_of(apns)["title"] == "I Love Boosters (2026)"


@respx.mock
async def test_an_admins_own_grab_does_not_notify_them(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """The rule that keeps push from being switched off within a day."""
    mock_seerr_auth(user_id=1, username="owner", permissions=ADMIN_PERMISSIONS)
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    apns = mock_apns()

    admin = await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    # /register resolved the caller to the same row the device belongs to.
    assert (await the_calling_user(db, "owner")).id == admin.id

    action = await make_action(db, "Stream Now")
    await grant(db, admin, action)

    response = await client.post(
        "/grab",
        headers=plex_headers,
        json={
            "action_id": action.id,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
        },
    )

    assert response.status_code == 200
    assert not apns.called


@respx.mock
async def test_the_admin_apps_action_free_grab_notifies_nobody(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """An admin approving a request is doing their own work, for anyone's device."""
    mock_seerr_auth(
        user_id=9, username="manager", permissions=MANAGE_REQUESTS_PERMISSIONS
    )
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    apns = mock_apns()

    await an_admin_with_a_device(db, configured, apns_key_pem)

    response = await client.post(
        "/manager/grab",
        headers=plex_headers,
        json={
            "download_client_id": 3,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
        },
    )

    assert response.status_code == 200
    assert not apns.called


@respx.mock
async def test_a_grab_still_succeeds_when_apple_is_down(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    """A push is never on the critical path. The grab already happened."""
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(url__startswith=APNS_PRODUCTION).mock(
        side_effect=httpx.ConnectError("apple is unreachable")
    )

    await an_admin_with_a_device(db, configured, apns_key_pem)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Jane Dietrich"), action)

    response = await client.post(
        "/grab",
        headers=plex_headers,
        json={
            "action_id": action.id,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is True


@respx.mock
async def test_a_sandbox_device_is_pushed_to_apples_sandbox_host(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
    apns_key_pem: str,
) -> None:
    mock_seerr_auth(user_id=7, username="Jane Dietrich")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    production = mock_apns(APNS_PRODUCTION)
    sandbox = mock_apns(APNS_SANDBOX)

    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.commit()
    await register_device(db, admin, device_token=OTHER_TOKEN, environment="sandbox")
    await configure_apns(db, configured, apns_key_pem)

    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Jane Dietrich"), action)

    await client.post(
        "/grab",
        headers=plex_headers,
        json={
            "action_id": action.id,
            "release_guid": "guid-1",
            "indexer_id": 1,
            "release_title": "I.Love.Boosters.2026.1080p.WEB-DL-FLUX",
        },
    )

    assert sandbox.called
    assert not production.called
