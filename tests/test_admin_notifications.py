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
from cplus_service.notify.types import NotificationType

from .conftest import (
    RELAY_API_KEY,
    RELAY_ENROL_URL,
    RELAY_INSTANCE_ID,
    RELAY_PUSH_URL,
    enable_notifications,
    enrolled,
    register_device,
)
from .test_admin_webui import signed_in

DEVICE_TOKEN = "ab" * 32


def relayed(**body) -> httpx.Response:
    return httpx.Response(200, json={"result": "delivered", **body})


def mock_enrol(**overrides) -> respx.Route:
    return respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(201, json=enrolled(**overrides))
    )


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
    assert "relay.test" in body


async def test_the_page_offers_no_way_to_configure_a_relay(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The fiasco this change removes.

    Nobody self-hosting will ever run their own relay, so a URL box was a field
    with one possible value, and the API key next to it was a credential that
    protects nothing the admin chose — isolation comes from token custody. Both
    are gone; enabling is the whole of setup.
    """
    await signed_in(client, db)
    await enable_notifications(db, configured)

    body = (await client.get("/admin/notifications")).text

    assert "Relay URL" not in body
    assert "Relay API key" not in body
    assert 'name="relay_url"' not in body
    assert 'name="relay_api_key"' not in body


async def test_everything_else_is_hidden_while_notifications_are_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Greyed-out controls invite an admin to fill them in and wonder why nothing happens."""
    await signed_in(client, db)

    body = (await client.get("/admin/notifications")).text

    assert "Send a test notification" not in body
    assert "A user requested something" not in body
    assert "Notifications are off" in body


@respx.mock
async def test_turning_it_on_enrols_and_reveals_what_it_governs(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Ticking one box is the entire setup. No key is ever typed."""
    route = mock_enrol()
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/enabled", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert route.called
    assert "A user requested something" in response.text

    await db.refresh(configured)
    assert configured.notifications_enabled is True
    assert configured.notification_relay_api_key == RELAY_API_KEY
    assert configured.notification_relay_instance_id == RELAY_INSTANCE_ID


@respx.mock
async def test_a_failed_enrollment_leaves_the_switch_off_and_says_why(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Switching on anyway would produce an install that reports itself
    capable, accepts device registrations, and silently sends nothing."""
    respx.post(RELAY_ENROL_URL).mock(side_effect=httpx.ConnectError("no route"))
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/enabled", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert "Could not reach" in response.text
    # The checkbox comes back unticked, because the server is the authority.
    assert "checked" not in response.text

    await db.refresh(configured)
    assert configured.notifications_enabled is False
    assert configured.notification_relay_api_key is None


@respx.mock
async def test_enrolling_happens_once_not_on_every_toggle(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Toggling while looking at something else must not burn a fresh identity."""
    route = mock_enrol()
    await signed_in(client, db)

    await client.post("/admin/notifications/enabled", data={"enabled": "on"})
    await client.post("/admin/notifications/enabled", data={"enabled": ""})
    await client.post("/admin/notifications/enabled", data={"enabled": "on"})

    assert route.call_count == 1


@respx.mock
async def test_an_unready_relay_enables_but_warns(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """An admin who did everything right deserves to know it is not their end."""
    mock_enrol(ready=False)
    await signed_in(client, db)

    response = await client.post(
        "/admin/notifications/enabled", data={"enabled": "on"}
    )

    assert "no Apple signing key" in response.text
    await db.refresh(configured)
    assert configured.notifications_enabled is True


async def test_turning_it_off_hides_them_again(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/enabled", data={"enabled": ""})

    assert "Send a test notification" not in response.text
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


async def test_the_page_shows_who_this_instance_is_to_the_relay(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Display only, so a support conversation has something to name."""
    await signed_in(client, db)
    await enable_notifications(db, configured)

    body = (await client.get("/admin/notifications")).text

    assert "Connected to" in body
    assert RELAY_INSTANCE_ID in body


async def test_the_page_says_so_when_enrollment_never_took(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await enable_notifications(db, configured, api_key=None)

    body = (await client.get("/admin/notifications")).text

    assert "Not connected" in body


async def test_the_relay_key_is_never_rendered_into_the_page(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Same discipline as the Prowlarr API key — and now nothing shows a field
    for it either, so there is nowhere it could leak from."""
    await signed_in(client, db)
    await enable_notifications(db, configured)

    body = (await client.get("/admin/notifications")).text

    assert RELAY_API_KEY not in body


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
# Reconnecting
# --------------------------------------------------------------------------- #


@respx.mock
async def test_reconnect_replaces_the_relay_identity(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The recovery path for a key the relay stopped accepting.

    Nothing can be repaired in place — the relay stores no keys, so there is
    nothing to look up — which makes re-enrolling both the fix and the only fix.
    """
    respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(
            201, json=enrolled(instance_id="freshid", api_key="canopy_freshid_abc")
        )
    )
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/reconnect")

    assert "Reconnected as freshid" in response.text
    await db.refresh(configured)
    assert configured.notification_relay_instance_id == "freshid"
    assert configured.notification_relay_api_key == "canopy_freshid_abc"


@respx.mock
async def test_reconnect_leaves_devices_alone(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """A new identity is only a new name in a rate-limit bucket."""
    respx.post(RELAY_ENROL_URL).mock(return_value=httpx.Response(201, json=enrolled()))
    admin = await signed_in(client, db)
    await enable_notifications(db, configured)
    await register_device(db, admin, device_token=DEVICE_TOKEN)

    await client.post("/admin/notifications/reconnect")

    assert (await db.execute(select(ApnsDevice))).scalars().first() is not None


@respx.mock
async def test_reconnect_reports_a_relay_that_will_not_have_us(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    respx.post(RELAY_ENROL_URL).mock(return_value=httpx.Response(403))
    await signed_in(client, db)
    await enable_notifications(db, configured)

    response = await client.post("/admin/notifications/reconnect")

    assert "not issuing new keys" in response.text


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
