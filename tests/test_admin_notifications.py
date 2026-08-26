"""The Notifications tab.

Drives the real templates, so a page that will not render shows up here rather
than in a browser. The assertions are about what an admin can see and do: the
switches persist, a bad key is refused while they are looking at it, and the
test button says something they can act on.
"""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ApnsDevice, Config, NotificationPreference, User
from cplus_service.notify import prefs
from cplus_service.notify.types import NotificationType

from .conftest import (
    APNS_BUNDLE_ID,
    APNS_KEY_ID,
    APNS_PRODUCTION,
    APNS_TEAM_ID,
    configure_apns,
    register_device,
)
from .test_admin_webui import signed_in

DEVICE_TOKEN = "ab" * 32


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


async def test_the_tab_is_gated_like_every_other_admin_page(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/admin/notifications", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_the_page_renders_a_switch_and_a_preview_for_every_type(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.get("/admin/notifications")

    assert response.status_code == 200
    body = response.text
    assert "A user requested something" in body
    assert "A user performed an action" in body
    # The preview is the only place the shape of a notification is visible
    # before one arrives.
    assert "Requested by Robin Example" in body
    assert "Robin Example: Stream Now" in body


async def test_the_page_says_push_is_off_before_it_is_configured(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The state this ships in until the signing key arrives."""
    await signed_in(client, db)

    response = await client.get("/admin/notifications")

    assert "Push is off until all four fields are set" in response.text


async def test_the_page_says_push_is_on_once_it_is_configured(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)

    response = await client.get("/admin/notifications")

    assert "Push is configured" in response.text


async def test_a_saved_key_is_never_rendered_back_into_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    """Same discipline as the Prowlarr API key."""
    await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)

    response = await client.get("/admin/notifications")

    assert "BEGIN PRIVATE KEY" not in response.text
    assert "(unchanged)" in response.text
    # The non-secret half is shown, so an admin can check it.
    assert APNS_TEAM_ID in response.text


# --------------------------------------------------------------------------- #
# The switches
# --------------------------------------------------------------------------- #


async def test_a_switch_round_trips(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    off = await client.post(
        "/admin/notifications/types/user_action", data={"enabled": ""}
    )
    assert off.status_code == 200
    assert "checked" not in off.text
    assert await prefs.is_enabled(db, NotificationType.USER_ACTION) is False

    on = await client.post(
        "/admin/notifications/types/user_action", data={"enabled": "on"}
    )
    assert "checked" in on.text
    assert await prefs.is_enabled(db, NotificationType.USER_ACTION) is True


async def test_a_switch_reflects_what_was_stored_on_the_next_page_load(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await client.post("/admin/notifications/types/user_requested", data={"enabled": ""})

    response = await client.get("/admin/notifications")

    row = await db.get(NotificationPreference, "user_requested")
    assert row is not None and row.enabled is False
    assert response.status_code == 200


async def test_an_unknown_type_is_a_404_rather_than_a_new_row(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/types/not_a_real_type", data={"enabled": "on"}
    )

    assert response.status_code == 404
    assert (await db.execute(select(NotificationPreference))).scalars().all() == []


# --------------------------------------------------------------------------- #
# The credentials form
# --------------------------------------------------------------------------- #


async def test_saving_all_four_fields_turns_push_on(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/apns",
        data={
            "apns_team_id": APNS_TEAM_ID,
            "apns_key_id": APNS_KEY_ID,
            "apns_bundle_id": APNS_BUNDLE_ID,
            "apns_private_key": apns_key_pem,
        },
    )

    assert response.status_code == 200
    assert "Push is configured" in response.text

    await db.refresh(configured)
    assert configured.apns_team_id == APNS_TEAM_ID
    assert configured.apns_private_key == apns_key_pem.strip()


async def test_saving_three_of_four_says_push_is_still_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/apns",
        data={
            "apns_team_id": APNS_TEAM_ID,
            "apns_key_id": APNS_KEY_ID,
            "apns_bundle_id": APNS_BUNDLE_ID,
            "apns_private_key": "",
        },
    )

    assert "Push stays off" in response.text


async def test_a_blank_key_field_keeps_the_stored_one(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    """So editing the team id cannot blank the key by accident."""
    await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)

    await client.post(
        "/admin/notifications/apns",
        data={
            "apns_team_id": "NEWTEAM123",
            "apns_key_id": APNS_KEY_ID,
            "apns_bundle_id": APNS_BUNDLE_ID,
            "apns_private_key": "",
        },
    )

    await db.refresh(configured)
    assert configured.apns_team_id == "NEWTEAM123"
    assert configured.apns_private_key == apns_key_pem


async def test_a_key_that_will_not_parse_is_refused_at_save_time(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Discovering this later as 'notifications quietly stopped' is far worse."""
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/apns",
        data={
            "apns_team_id": APNS_TEAM_ID,
            "apns_key_id": APNS_KEY_ID,
            "apns_bundle_id": APNS_BUNDLE_ID,
            "apns_private_key": "whatever was on the clipboard",
        },
    )

    assert "BEGIN and END" in response.text
    await db.refresh(configured)
    assert configured.apns_private_key is None


# --------------------------------------------------------------------------- #
# The test button
# --------------------------------------------------------------------------- #


async def test_the_test_button_explains_an_unconfigured_install(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/notifications/test")

    assert "Add the key, key id, team id and bundle id first" in response.text


async def test_the_test_button_explains_having_no_devices(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)

    response = await client.post("/admin/notifications/test")

    assert "No devices are registered" in response.text


@respx.mock
async def test_the_test_button_reaches_the_callers_own_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    """The exact opposite of the emitting rule, and right: it is the phone in their hand."""
    route = respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(200)
    )
    admin = await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)
    await register_device(db, admin, device_token=DEVICE_TOKEN)

    response = await client.post("/admin/notifications/test")

    assert route.called
    assert "Sent to 1 device." in response.text


@respx.mock
async def test_the_test_button_ignores_the_switches(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    """The question it asks is whether delivery works at all."""
    route = respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(200)
    )
    admin = await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)
    await register_device(db, admin, device_token=DEVICE_TOKEN)
    await prefs.set_enabled(db, NotificationType.USER_REQUESTED, False)
    await db.commit()

    await client.post("/admin/notifications/test")

    assert route.called


@respx.mock
async def test_the_test_button_reports_a_rejection(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(400, json={"reason": "BadTopic"})
    )
    admin = await signed_in(client, db)
    await configure_apns(db, configured, apns_key_pem)
    await register_device(db, admin, device_token=DEVICE_TOKEN)

    response = await client.post("/admin/notifications/test")

    assert "1 failed" in response.text


# --------------------------------------------------------------------------- #
# The device list
# --------------------------------------------------------------------------- #


async def test_the_page_explains_itself_when_no_device_is_registered(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """There is nothing to enter by hand, so the empty state has to say so."""
    await signed_in(client, db)

    response = await client.get("/admin/notifications")

    assert "The app registers itself" in response.text


async def test_a_registered_device_is_listed_with_its_owner(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    admin = await signed_in(client, db)
    await register_device(
        db, admin, device_token=DEVICE_TOKEN, device_name="Living Room Apple TV"
    )

    response = await client.get("/admin/notifications")

    assert "Living Room Apple TV" in response.text
    assert admin.plex_username in response.text
    # Shown truncated — the rest of a 64-character device address tells an
    # admin nothing — and the Remove form carries the whole of it in its body
    # rather than its URL, so it stays out of access logs and history.
    assert DEVICE_TOKEN[:8] in response.text
    assert f"/devices/{DEVICE_TOKEN}" not in response.text
    assert f'name="device_token" value="{DEVICE_TOKEN}"' in response.text


async def test_a_sandbox_device_is_marked_as_one(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    admin = await signed_in(client, db)
    await register_device(
        db, admin, device_token=DEVICE_TOKEN, environment="sandbox"
    )

    response = await client.get("/admin/notifications")

    assert "sandbox" in response.text


async def test_an_admin_can_remove_any_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Unlike the app's own sign-out, which is limited to the caller's device."""
    await signed_in(client, db)
    someone_else = User(seerr_user_id=99, plex_username="someone-else")
    db.add(someone_else)
    await db.commit()
    await register_device(db, someone_else, device_token=DEVICE_TOKEN)

    response = await client.post(
        "/admin/notifications/devices/delete",
        data={"device_token": DEVICE_TOKEN},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


async def test_removing_a_device_that_is_already_gone_still_redirects(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/devices/delete",
        data={"device_token": DEVICE_TOKEN},
        follow_redirects=False,
    )

    assert response.status_code == 303


async def test_deleting_a_user_takes_their_devices_with_them(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The ON DELETE CASCADE, which SQLite only honours with the pragma set."""
    await signed_in(client, db)
    user = User(seerr_user_id=99, plex_username="departing")
    db.add(user)
    await db.commit()
    await register_device(db, user, device_token=DEVICE_TOKEN)

    response = await client.post(
        f"/admin/users/{user.id}/delete", follow_redirects=False
    )

    assert response.status_code == 303
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []
