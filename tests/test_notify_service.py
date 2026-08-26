"""Who gets told, and who does not.

The policy tests. Each one names a rule from
:mod:`cplus_service.notify.service` that a well-meaning refactor could quietly
drop: the master switch, the per-type switches, the unconfigured no-op, the
actor exclusion, and cleaning up after a token the relay says is dead.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ApnsDevice, Config, NotificationPreference, User
from cplus_service.notify import prefs
from cplus_service.notify.messages import MediaSummary, user_requested
from cplus_service.notify.service import deliver, eligible_devices
from cplus_service.notify.types import NotificationType

from .conftest import (
    RELAY_API_KEY,
    RELAY_PUSH_URL,
    enable_notifications,
    register_device,
)

NOTIFICATION = user_requested(
    MediaSummary(title="The End of Oak Street", year=2026), username="Robin Example"
)


async def make_user(db: AsyncSession, seerr_user_id: int, username: str) -> User:
    user = User(seerr_user_id=seerr_user_id, plex_username=username)
    db.add(user)
    await db.commit()
    return user


async def dispatch(app: FastAPI, **kwargs):
    """Run a delivery against the app's own session factory and clients."""
    state = app.state.cplus
    return await deliver(
        sessionmaker=state.sessionmaker,
        http=state.relay_http,
        notification=NOTIFICATION,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


async def test_every_type_is_on_before_anyone_touches_anything(
    db: AsyncSession,
) -> None:
    """"Both enabled by default", with nothing seeded to make it true."""
    assert await prefs.current(db) == {
        NotificationType.USER_REQUESTED: True,
        NotificationType.USER_ACTION: True,
    }
    assert (await db.execute(select(NotificationPreference))).scalars().all() == []


async def test_a_type_can_be_switched_off_and_back_on(db: AsyncSession) -> None:
    await prefs.set_enabled(db, NotificationType.USER_ACTION, False)
    await db.commit()

    assert await prefs.is_enabled(db, NotificationType.USER_ACTION) is False
    assert await prefs.is_enabled(db, NotificationType.USER_REQUESTED) is True

    await prefs.set_enabled(db, NotificationType.USER_ACTION, True)
    await db.commit()
    assert await prefs.is_enabled(db, NotificationType.USER_ACTION) is True


async def test_choosing_on_is_stored_rather_than_left_to_the_default(
    db: AsyncSession,
) -> None:
    """So a later change to the default cannot rewrite a deliberate choice."""
    await prefs.set_enabled(db, NotificationType.USER_ACTION, True)
    await db.commit()

    row = await db.get(NotificationPreference, NotificationType.USER_ACTION.value)
    assert row is not None and row.enabled is True


async def test_a_leftover_row_for_an_unknown_type_is_ignored(
    db: AsyncSession,
) -> None:
    """A preference from a newer version must not become a switch nobody can explain."""
    db.add(NotificationPreference(notification_type="from_the_future", enabled=False))
    await db.commit()

    assert set(await prefs.current(db)) == set(NotificationType)


# --------------------------------------------------------------------------- #
# Who is eligible
# --------------------------------------------------------------------------- #


async def test_the_person_who_acted_is_excluded_on_every_device_they_own(
    db: AsyncSession,
) -> None:
    actor = await make_user(db, 1, "actor")
    other = await make_user(db, 2, "other")
    await register_device(db, actor, device_token="aa" * 16, device_name="phone")
    await register_device(db, actor, device_token="bb" * 16, device_name="apple tv")
    await register_device(db, other, device_token="cc" * 16)

    devices = await eligible_devices(db, exclude_user_id=actor.id)
    assert [d.device_token for d in devices] == ["cc" * 16]


async def test_with_no_actor_named_every_device_is_eligible(db: AsyncSession) -> None:
    """What the admin UI's test button relies on to reach the caller's own phone."""
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    assert len(await eligible_devices(db)) == 1


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def relayed(**body) -> httpx.Response:
    """A 200 from the relay. It answers 200 whenever Apple answered at all."""
    return httpx.Response(200, json={"result": "delivered", **body})


@respx.mock
async def test_nothing_is_sent_while_notifications_are_switched_off(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """The state every install ships in, and stays in until an admin opts in.

    Checked before the per-type switches on purpose: an install with the master
    switch off should read as off, not as "that type is off".
    """
    route = respx.post(RELAY_PUSH_URL)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason is not None
    assert "switched off for this instance" in report.skipped_reason


@respx.mock
async def test_nothing_is_sent_when_enrollment_never_succeeded(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """Switched on with no relay identity: on, and nowhere to send.

    Only reachable now if enrolling failed, since switching on is what enrols.
    """
    route = respx.post(RELAY_PUSH_URL)
    await enable_notifications(db, configured, api_key=None)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason is not None
    assert "not connected to the notification relay" in report.skipped_reason


@respx.mock
async def test_nothing_is_sent_when_the_type_is_switched_off(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    route = respx.post(RELAY_PUSH_URL)
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    await prefs.set_enabled(db, NotificationType.USER_REQUESTED, False)
    await db.commit()

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason is not None
    assert "switched off" in report.skipped_reason


@respx.mock
async def test_nothing_is_sent_when_no_device_is_registered(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    route = respx.post(RELAY_PUSH_URL)
    await enable_notifications(db, configured)

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason == "No devices are registered for push."


@respx.mock
async def test_a_configured_install_pushes_to_each_device(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    await enable_notifications(db, configured)

    user = await make_user(db, 1, "someone")
    await register_device(db, user, device_token="aa" * 16)
    await register_device(db, user, device_token="bb" * 16, environment="sandbox")

    report = await dispatch(app)

    assert report.delivered == 2
    assert report.failed == 0
    assert route.call_count == 2


@respx.mock
async def test_the_relay_is_sent_text_and_the_device_token_only(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """Not an APNs payload — the relay builds `aps` itself and refuses ours.

    Also the shape of what the relay operator can see, which is what the
    Notifications tab promises an admin.
    """
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user, device_token="aa" * 16, environment="sandbox")

    await dispatch(app)

    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {RELAY_API_KEY}"
    assert json.loads(request.read()) == {
        "device_token": "aa" * 16,
        "environment": "sandbox",
        "title": "The End of Oak Street (2026)",
        "subtitle": "Requested by Robin Example",
        # Opaque to the relay, which forwards it under a `canopy` key for the
        # app to route on when someone taps the notification.
        "data": {"type": "user_requested"},
    }


@respx.mock
async def test_a_device_apple_no_longer_knows_is_deleted(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """The relay stores no tokens, so if this side ignores it nothing cleans up."""
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(
            200, json={"result": "unregistered", "reason": "Unregistered"}
        )
    )
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.unregistered == 1
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


@respx.mock
async def test_a_failing_device_does_not_stop_the_others(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    respx.post(RELAY_PUSH_URL, json__device_token="aa" * 16).mock(
        return_value=httpx.Response(200, json={"result": "failed", "reason": "BadTopic"})
    )
    respx.post(RELAY_PUSH_URL, json__device_token="bb" * 16).mock(
        return_value=relayed()
    )
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user, device_token="aa" * 16)
    await register_device(db, user, device_token="bb" * 16)

    report = await dispatch(app)

    assert report.delivered == 1
    assert report.failed == 1


@respx.mock
async def test_a_relay_that_rejects_our_key_is_a_failure_not_a_deletion(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """A 401 says nothing about the device token, and must never be read as if it did."""
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(401, json={"detail": "This relay API key is not valid."})
    )
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.failed == 1
    assert report.unregistered == 0
    assert (await db.execute(select(ApnsDevice))).scalars().first() is not None


@respx.mock
async def test_an_unreachable_relay_is_reported_rather_than_raised(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """Delivery runs after the response; there is no caller left to catch anything."""
    respx.post(RELAY_PUSH_URL).mock(side_effect=httpx.ConnectError("no route"))
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.failed == 1
    assert report.delivered == 0


@respx.mock
async def test_an_unrecognised_result_is_not_treated_as_delivered(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """Guessing 'probably fine' from a relay speaking a dialect we do not know
    is how a dead device token stays in the table forever."""
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "sideways"})
    )
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.failed == 1
    assert (await db.execute(select(ApnsDevice))).scalars().first() is not None


@respx.mock
async def test_the_actor_is_excluded_end_to_end(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    await enable_notifications(db, configured)
    actor = await make_user(db, 1, "actor")
    await register_device(db, actor)

    report = await dispatch(app, exclude_user_id=actor.id)

    assert not route.called
    assert report.skipped_reason == "No devices are registered for push."


@pytest.mark.parametrize("environment", ["sandbox", "production"])
@respx.mock
async def test_the_devices_environment_is_passed_through_untouched(
    app: FastAPI,
    db: AsyncSession,
    configured: Config,
    environment: str,
) -> None:
    """The relay picks Apple's host from this; only this side knows which build."""
    route = respx.post(RELAY_PUSH_URL).mock(return_value=relayed())
    await enable_notifications(db, configured)
    user = await make_user(db, 1, "someone")
    await register_device(db, user, environment=environment)

    report = await dispatch(app)

    assert report.delivered == 1
    assert json.loads(route.calls[0].request.read())["environment"] == environment
