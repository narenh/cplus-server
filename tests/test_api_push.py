"""Device registration, and the two paths that actually raise a notification.

End-to-end through the real app: a grab or a request goes in, and a push comes
out at the relay's mocked door with the right two lines on it. These are the
tests that would catch an emitter being dropped in a refactor, which the unit
tests above them cannot see.
"""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Action, ApnsDevice, Config, User

from .conftest import (
    PROWLARR_URL,
    RELAY_PUSH_URL,
    SEERR_URL,
    enable_notifications,
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


def mock_relay() -> respx.Route:
    return respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "delivered"})
    )


async def authenticate(client: httpx.AsyncClient, headers: dict) -> None:
    assert (await client.get("/register", headers=headers)).status_code == 200


def lines_of(route: respx.Route) -> dict:
    """The two display lines of the last notification handed to the relay.

    Not an ``aps.alert``: the relay builds that. What leaves this service is
    text, which is also exactly what the relay operator can see.
    """
    body = json.loads(route.calls.last.request.content)
    return {"title": body["title"], "subtitle": body["subtitle"]}


# --------------------------------------------------------------------------- #
# POST /manager/push-devices
# --------------------------------------------------------------------------- #


@respx.mock
async def test_registering_is_refused_while_notifications_are_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Otherwise the app holds a token it believes is live, and this install
    accumulates device tokens for a feature nobody switched on."""
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)

    response = await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    assert response.status_code == 409
    assert "/capabilities" in response.json()["detail"]
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


@respx.mock
async def test_an_admin_can_register_a_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)
    await enable_notifications(db, configured)

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
    await enable_notifications(db, configured)

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
    await enable_notifications(db, configured)
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
    await enable_notifications(db, configured)

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
    await enable_notifications(db, configured)
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
async def test_unregistering_still_works_after_notifications_are_switched_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Sign-out is never gated on the feature being on.

    An app signing out, or repointing at a different instance, has to be able
    to hand its token back to the instance it is leaving — otherwise a stale
    registration survives exactly the situation it most needs to be cleared in.
    """
    mock_seerr_auth(permissions=ADMIN_PERMISSIONS)
    await enable_notifications(db, configured)
    await client.post(
        "/manager/push-devices",
        headers=plex_headers,
        json={"device_token": DEVICE_TOKEN},
    )

    configured.notifications_enabled = False
    db.add(configured)
    await db.commit()

    response = await client.delete(
        f"/manager/push-devices/{DEVICE_TOKEN}", headers=plex_headers
    )

    assert response.status_code == 200
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


@respx.mock
async def test_one_admin_cannot_unregister_anothers_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    """Sign-out is for your own device; the admin console is where the rest lives."""
    mock_seerr_auth(user_id=1, username="first", permissions=ADMIN_PERMISSIONS)
    await enable_notifications(db, configured)
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
    *,
    device_token: str = DEVICE_TOKEN,
    environment: str = "production",
) -> User:
    """An admin holding a registered device, on an install with push switched on."""
    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.commit()
    await register_device(
        db, admin, device_token=device_token, environment=environment
    )
    await enable_notifications(db, configured)
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
) -> None:
    """The whole point, end to end: someone requests, the admin's phone says so."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99, "status": 1})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Robin Example"))

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
    assert relay.called
    assert lines_of(relay) == {
        "title": "The End of Oak Street (2026)",
        "subtitle": "Requested by Robin Example",
    }


@respx.mock
async def test_a_request_without_a_title_falls_back_to_the_tmdb_id(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """Ugly, and better than either a TMDB round trip or the word 'Unknown'."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Robin Example"))

    await client.post(
        "/request", headers=plex_headers, json={"tmdb_id": 603, "type": "movie"}
    )

    assert lines_of(relay)["title"] == "TMDB 603"


@respx.mock
async def test_a_request_seerr_rejects_notifies_nobody(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """Nothing happened, so there is nothing to be told about."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(409, json={"message": "Already requested"})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    await grant_request_action(db, await the_calling_user(db, "Robin Example"))

    response = await client.post(
        "/request", headers=plex_headers, json={"tmdb_id": 603, "type": "movie"}
    )

    assert response.status_code == 409
    assert not relay.called


# --------------------------------------------------------------------------- #
# POST /grab raises a notification; POST /manager/grab does not
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_action_grab_notifies_the_admin(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Robin Example"), action)

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
    assert lines_of(relay) == {
        "title": "I Love Boosters (2026)",
        "subtitle": "Robin Example: Stream Now",
    }


@respx.mock
async def test_a_grab_without_a_title_falls_back_to_the_release_name(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """An older client that has not learned to send media_title still reads well."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Robin Example"), action)

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

    assert lines_of(relay)["title"] == "I Love Boosters (2026)"


@respx.mock
async def test_an_admins_own_grab_does_not_notify_them(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """The rule that keeps push from being switched off within a day."""
    mock_seerr_auth(user_id=1, username="owner", permissions=ADMIN_PERMISSIONS)
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    relay = mock_relay()

    admin = await an_admin_with_a_device(db, configured)
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
    assert not relay.called


@respx.mock
async def test_the_admin_apps_action_free_grab_notifies_nobody(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """An admin approving a request is doing their own work, for anyone's device."""
    mock_seerr_auth(
        user_id=9, username="manager", permissions=MANAGE_REQUESTS_PERMISSIONS
    )
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)

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
    assert not relay.called


@respx.mock
async def test_a_grab_still_succeeds_when_the_relay_is_down(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """A push is never on the critical path. The grab already happened."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(RELAY_PUSH_URL).mock(
        side_effect=httpx.ConnectError("the relay is unreachable")
    )

    await an_admin_with_a_device(db, configured)
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Robin Example"), action)

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
async def test_a_sandbox_device_tells_the_relay_which_apple_host_to_use(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """Only this side knows which build a token came from; the relay picks the host."""
    mock_seerr_auth(user_id=7, username="Robin Example")
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json={})
    )
    relay = mock_relay()

    await an_admin_with_a_device(
        db, configured, device_token=OTHER_TOKEN, environment="sandbox"
    )
    await authenticate(client, plex_headers)
    action = await make_action(db, "Stream Now")
    await grant(db, await the_calling_user(db, "Robin Example"), action)

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

    body = json.loads(relay.calls.last.request.content)
    assert body["environment"] == "sandbox"
    assert body["device_token"] == OTHER_TOKEN
