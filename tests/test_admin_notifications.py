"""The Notifications tab.

Drives the real templates, so a page that will not render shows up here rather
than in a browser. The assertions are about what an admin can see and do: the
master switch hides everything it governs, the switches persist, a saved relay
key is never shown back, and every button says something they can act on.
"""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ApnsDevice, Config, NotificationPreference, User
from cplus_service.notify import prefs
from cplus_service.notify.relay import DEFAULT_RELAY_URL
from cplus_service.notify.types import NotificationType

from .conftest import (
    RELAY_API_KEY,
    RELAY_PUSH_URL,
    RELAY_URL,
    RELAY_VERIFY_URL,
    enable_notifications,
    register_device,
)
from .test_admin_webui import signed_in

DEVICE_TOKEN = "ab" * 32


def relayed(**body) -> httpx.Response:
    return httpx.Response(200, json={"result": "delivered", **body})


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


async def test_the_tab_is_gated_like_every_other_admin_page(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/admin/notifications", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_notifications_are_off_on_a_fresh_install(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The one setting in the admin UI that is off by default."""
    await signed_in(client, db)

    response = await client.get("/admin/notifications")

    assert response.status_code == 200
    assert 'name="enabled"' in response.text
    assert "checked" not in response.text
    assert configured.notifications_enabled is False


async def test_the_page_says_what_enabling_commits_to(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """On the page, not in the docs. Nobody reads the docs before ticking a box."""
    await signed_in(client, db)

    body = (await client.get("/admin/notifications")).text

    assert "plaintext" in body
    assert DEFAULT_RELAY_URL.removeprefix("https://") in body


async def test_everything_else_is_hidden_while_notifications_are_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Greyed-out controls invite an admin to fill them in and wonder why nothing happens."""
    await signed_in(client, db)

    body = (await client.get("/admin/notifications")).text

    assert "Relay API key" not in body
    assert "A user requested something" not in body
    assert "Notifications are off" in body


async def test_turning_it_on_reveals_the_settings_it_governs(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/enabled", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert "Relay API key" in response.text
    assert "A user requested something" in response.text
    await db.refresh(configured)
    assert configured.notifications_enabled is True


async def test_turning_it_off_hides_them_again(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/enabled", data={"enabled": ""})

    assert "Relay API key" not in response.text
    await db.refresh(configured)
    assert configured.notifications_enabled is False


async def test_turning_it_off_does_not_throw_away_registered_devices(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """An admin toggling this while investigating should not silently cost every
    admin their registration, with no way back but asking each to relaunch."""
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
    await register_device(db, admin, device_token=DEVICE_TOKEN)

    await client.post("/admin/notifications/enabled", data={"enabled": ""})

    assert (await db.execute(select(ApnsDevice))).scalars().first() is not None


async def test_the_page_renders_a_switch_and_a_preview_for_every_type(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.get("/admin/notifications")

    assert response.status_code == 200
    body = response.text
    assert "A user requested something" in body
    assert "A user performed an action" in body
    # The preview is the only place the shape of a notification is visible
    # before one arrives.
    assert "Requested by Robin Example" in body
    assert "Robin Example: Stream Now" in body


async def test_the_page_says_nothing_is_sent_before_a_key_is_saved(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured, api_key=None)

    response = await client.get("/admin/notifications")

    assert "Nothing is sent until an API key is saved" in response.text


async def test_a_saved_relay_key_is_never_rendered_back_into_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Same discipline as the Prowlarr API key."""
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.get("/admin/notifications")

    assert RELAY_API_KEY not in response.text
    assert "(unchanged)" in response.text
    # The non-secret half is shown, so an admin can check where it is pointed.
    assert RELAY_URL in response.text


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
    await enable_notifications(db, configured)
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
# The relay form
# --------------------------------------------------------------------------- #


async def test_saving_a_url_and_key_configures_the_relay(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured, api_key=None)

    response = await client.post(
        "/admin/notifications/relay",
        data={"relay_url": RELAY_URL, "relay_api_key": RELAY_API_KEY},
    )

    assert response.status_code == 200
    assert "Check the relay" in response.text

    await db.refresh(configured)
    assert configured.notification_relay_url == RELAY_URL
    assert configured.notification_relay_api_key == RELAY_API_KEY


async def test_saving_without_a_key_says_nothing_will_be_sent(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured, api_key=None)

    response = await client.post(
        "/admin/notifications/relay", data={"relay_url": RELAY_URL, "relay_api_key": ""}
    )

    assert "Nothing will be sent until an API key is set" in response.text


async def test_a_blank_key_field_keeps_the_stored_one(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """So editing the URL cannot blank the key by accident."""
    await signed_in(client, db)
    await enable_notifications(db, configured)

    await client.post(
        "/admin/notifications/relay",
        data={"relay_url": "https://other.relay.test", "relay_api_key": ""},
    )

    await db.refresh(configured)
    assert configured.notification_relay_url == "https://other.relay.test"
    assert configured.notification_relay_api_key == RELAY_API_KEY


async def test_a_blank_url_resets_to_the_default_rather_than_nothing(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """A key with nowhere to send it is not a state anyone means to be in."""
    await signed_in(client, db)
    await enable_notifications(db, configured)

    await client.post(
        "/admin/notifications/relay", data={"relay_url": "", "relay_api_key": ""}
    )

    await db.refresh(configured)
    assert configured.notification_relay_url == DEFAULT_RELAY_URL


# --------------------------------------------------------------------------- #
# Checking the relay
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_check_button_reports_a_working_key(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(RELAY_VERIFY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "instance": "notcanopy",
                "bundle_id": "com.example.cplus",
                "ready": True,
            },
        )
    )
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/relay/check")

    assert "notcanopy" in response.text


@respx.mock
async def test_the_check_button_reports_a_rejected_key(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.get(RELAY_VERIFY_URL).mock(return_value=httpx.Response(401))
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/relay/check")

    assert "does not recognise this API key" in response.text


@respx.mock
async def test_the_check_button_says_when_the_relay_is_the_one_not_ready(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The failure an admin would otherwise spend an afternoon re-pasting a good key over."""
    respx.get(RELAY_VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "instance": "x", "ready": False})
    )
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/relay/check")

    assert "Nothing is wrong on this end" in response.text


async def test_the_check_button_explains_a_missing_key(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured, api_key=None)

    response = await client.post("/admin/notifications/relay/check")

    assert "No relay API key is set" in response.text


# --------------------------------------------------------------------------- #
# The test button
# --------------------------------------------------------------------------- #


async def test_the_test_button_refuses_while_notifications_are_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The master switch is consent to use the relay, not a preference about
    which notifications to send. A button that routed text through a third
    party after an admin declined would be a straightforward betrayal of it."""
    await signed_in(client, db)

    response = await client.post("/admin/notifications/test")

    assert "switched off for this instance" in response.text


async def test_the_test_button_explains_having_no_devices(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/test")

    assert "No devices are registered" in response.text


@respx.mock
async def test_the_test_button_reaches_the_callers_own_device(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The exact opposite of the emitting rule, and right: it is the phone in their hand."""
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
    await register_device(db, admin, device_token=DEVICE_TOKEN)

    response = await client.post("/admin/notifications/test")

    assert route.called
    assert "Sent to 1 device." in response.text


@respx.mock
async def test_the_test_button_ignores_the_per_type_switches(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The question it asks is whether delivery works at all."""
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
    await register_device(db, admin, device_token=DEVICE_TOKEN)
    await prefs.set_enabled(db, NotificationType.USER_REQUESTED, False)
    await db.commit()

    await client.post("/admin/notifications/test")

    assert route.called


@respx.mock
async def test_the_test_button_reports_a_rejection(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "failed", "reason": "BadTopic"})
    )
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
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
    await enable_notifications(db, configured)

    response = await client.get("/admin/notifications")

    assert "The app registers itself" in response.text


async def test_a_registered_device_is_listed_with_its_owner(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
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
    await enable_notifications(db, configured)
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
