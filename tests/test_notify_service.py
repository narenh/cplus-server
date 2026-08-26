"""Who gets told, and who does not.

The policy tests. Each one names a rule from
:mod:`cplus_service.notify.service` that a well-meaning refactor could quietly
drop: the switches, the unconfigured no-op, the actor exclusion, and cleaning
up after Apple.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import ApnsDevice, Config, NotificationPreference, User
from cplus_service.notify import prefs
from cplus_service.notify.apns import ProviderTokenCache
from cplus_service.notify.messages import MediaSummary, user_requested
from cplus_service.notify.service import deliver, eligible_devices
from cplus_service.notify.types import NotificationType

from .conftest import APNS_PRODUCTION, APNS_SANDBOX, configure_apns, register_device

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
        http=state.apns_http,
        tokens=ProviderTokenCache(),
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


@respx.mock
async def test_nothing_is_sent_while_push_is_unconfigured(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """The state this ships in until the signing key arrives."""
    route = respx.post(url__startswith=APNS_PRODUCTION)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason is not None
    assert "not configured" in report.skipped_reason


@respx.mock
async def test_nothing_is_sent_when_the_type_is_switched_off(
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    route = respx.post(url__startswith=APNS_PRODUCTION)
    await configure_apns(db, configured, apns_key_pem)
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
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    route = respx.post(url__startswith=APNS_PRODUCTION)
    await configure_apns(db, configured, apns_key_pem)

    report = await dispatch(app)

    assert not route.called
    assert report.skipped_reason == "No devices are registered for push."


@respx.mock
async def test_a_configured_install_pushes_to_each_device(
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(200)
    )
    respx.post(url__startswith=APNS_SANDBOX).mock(return_value=httpx.Response(200))
    await configure_apns(db, configured, apns_key_pem)

    user = await make_user(db, 1, "someone")
    await register_device(db, user, device_token="aa" * 16)
    await register_device(db, user, device_token="bb" * 16, environment="sandbox")

    report = await dispatch(app)

    assert report.delivered == 2
    assert report.failed == 0


@respx.mock
async def test_a_device_apple_no_longer_knows_is_deleted(
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    """The only correct response to a 410 — the token will never work again."""
    respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(410, json={"reason": "Unregistered"})
    )
    await configure_apns(db, configured, apns_key_pem)
    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.unregistered == 1
    assert (await db.execute(select(ApnsDevice))).scalars().all() == []


@respx.mock
async def test_a_failing_device_does_not_stop_the_others(
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    respx.post(url__startswith=f"{APNS_PRODUCTION}/3/device/{'aa' * 16}").mock(
        return_value=httpx.Response(400, json={"reason": "BadTopic"})
    )
    respx.post(url__startswith=f"{APNS_PRODUCTION}/3/device/{'bb' * 16}").mock(
        return_value=httpx.Response(200)
    )
    await configure_apns(db, configured, apns_key_pem)
    user = await make_user(db, 1, "someone")
    await register_device(db, user, device_token="aa" * 16)
    await register_device(db, user, device_token="bb" * 16)

    report = await dispatch(app)

    assert report.delivered == 1
    assert report.failed == 1


@respx.mock
async def test_an_unusable_key_is_reported_rather_than_raised(
    app: FastAPI, db: AsyncSession, configured: Config
) -> None:
    """Delivery runs after the response; there is no caller left to catch anything."""
    configured.apns_private_key = "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----"
    configured.apns_team_id = "TEAM123456"
    configured.apns_key_id = "KEY1234567"
    configured.apns_bundle_id = "com.example.cplus"
    db.add(configured)
    await db.commit()

    user = await make_user(db, 1, "someone")
    await register_device(db, user)

    report = await dispatch(app)

    assert report.delivered == 0
    assert report.skipped_reason is not None


@respx.mock
async def test_the_actor_is_excluded_end_to_end(
    app: FastAPI, db: AsyncSession, configured: Config, apns_key_pem: str
) -> None:
    route = respx.post(url__startswith=APNS_PRODUCTION).mock(
        return_value=httpx.Response(200)
    )
    await configure_apns(db, configured, apns_key_pem)
    actor = await make_user(db, 1, "actor")
    await register_device(db, actor)

    report = await dispatch(app, exclude_user_id=actor.id)

    assert not route.called
    assert report.skipped_reason == "No devices are registered for push."


@pytest.mark.parametrize("environment", ["sandbox", "production"])
@respx.mock
async def test_each_environment_reaches_its_own_apple_host(
    app: FastAPI,
    db: AsyncSession,
    configured: Config,
    apns_key_pem: str,
    environment: str,
) -> None:
    host = APNS_SANDBOX if environment == "sandbox" else APNS_PRODUCTION
    route = respx.post(url__startswith=host).mock(return_value=httpx.Response(200))
    await configure_apns(db, configured, apns_key_pem)
    user = await make_user(db, 1, "someone")
    await register_device(db, user, environment=environment)

    report = await dispatch(app)

    assert report.delivered == 1
    assert route.called
