"""``POST /webhooks/seerr`` — requests filed in Seerr instead of in the app.

Two halves. The first is the parser, which is where most of the ways a delivery
can be strange are handled: Seerr's payload is a user-editable template whose
values all arrive as strings. The second drives the real endpoint, because the
things worth breaking are the ones no unit test can see — the secret check, the
activity row, and the push going out exactly once for a request that arrived
through both doors.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import (
    Action,
    ActivityLog,
    Config,
    EventType,
    SeerrRequestNotice,
    User,
)
from cplus_service.seerr.webhook import parse_event, split_year

from .conftest import (
    RELAY_PUSH_URL,
    SEERR_URL,
    enable_notifications,
    grant,
    register_device,
    seerr_user_payload,
)

SECRET = "wh_secret_2Zq8fN4rLpX7vKdMwEbGhT1sYcRu0aJi"
WEBHOOK = "/webhooks/seerr"
DEVICE_TOKEN = "ab" * 32


def webhook_payload(
    *,
    notification_type: str = "MEDIA_PENDING",
    subject: str | None = "The End of Oak Street (2026)",
    request_id: Any = 99,
    username: str | None = "Robin Example",
    email: str | None = "robin@example.com",
    tmdb_id: Any = 603,
    media_type: str | None = "movie",
) -> dict[str, Any]:
    """Seerr's stock JSON template, as it arrives once Seerr has filled it in.

    Everything is a string, including the two ids, because the template is
    substituted into JSON *text*. That is the normal case, not a broken one.
    """
    request: dict[str, Any] = {"requestedBy_avatar": "https://plex.tv/avatar"}
    if request_id is not None:
        request["request_id"] = str(request_id)
    if username is not None:
        request["requestedBy_username"] = username
    if email is not None:
        request["requestedBy_email"] = email

    media: dict[str, Any] = {"status": "PENDING", "status4k": "UNKNOWN"}
    if tmdb_id is not None:
        media["tmdbId"] = str(tmdb_id)
    if media_type is not None:
        media["media_type"] = media_type

    return {
        "notification_type": notification_type,
        "event": "Pending Request",
        "subject": subject,
        "message": "A film about a street.",
        "image": "https://image.tmdb.org/t/p/w600/oak.jpg",
        "media": media,
        "request": request,
        "extra": [],
    }


def auth(secret: str = SECRET) -> dict[str, str]:
    return {"Authorization": secret}


async def webhook_enabled(db: AsyncSession, config: Config) -> Config:
    config.seerr_webhook_secret = SECRET
    db.add(config)
    await db.commit()
    return config


async def an_admin_with_a_device(db: AsyncSession, config: Config) -> User:
    """An admin holding a registered device, on an install with push on."""
    admin = User(seerr_user_id=1, plex_username="owner")
    db.add(admin)
    await db.commit()
    await register_device(db, admin, device_token=DEVICE_TOKEN)
    await enable_notifications(db, config)
    return admin


def mock_relay() -> respx.Route:
    return respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "delivered"})
    )


def lines_of(route: respx.Route) -> dict[str, str]:
    body = json.loads(route.calls.last.request.content)
    return {"title": body["title"], "subtitle": body["subtitle"]}


async def request_rows(db: AsyncSession) -> list[ActivityLog]:
    rows = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.event_type == EventType.REQUEST)
        .order_by(ActivityLog.id)
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Reading the payload
# --------------------------------------------------------------------------- #


def test_the_stock_payload_reads_as_a_filed_request() -> None:
    event = parse_event(webhook_payload())

    assert event.is_request_filed
    assert not event.is_test
    assert event.request_id == 99
    assert event.tmdb_id == 603
    assert event.username == "Robin Example"
    assert event.email == "robin@example.com"
    assert event.media_type == "movie"
    assert event.subject == "The End of Oak Street (2026)"


def test_an_auto_approved_request_is_still_a_filed_request() -> None:
    """Seerr approved it on the spot; somebody still asked for something."""
    assert parse_event(
        webhook_payload(notification_type="MEDIA_AUTO_APPROVED")
    ).is_request_filed


@pytest.mark.parametrize(
    "notification_type",
    ["MEDIA_APPROVED", "MEDIA_AVAILABLE", "MEDIA_DECLINED", "ISSUE_CREATED"],
)
def test_the_other_notification_types_are_not_requests(notification_type: str) -> None:
    event = parse_event(webhook_payload(notification_type=notification_type))
    assert not event.is_request_filed
    assert not event.is_test


#: The minimum payload the README tells an admin with a customised template to
#: keep, substituted. Kept here so the documented template cannot quietly stop
#: being a working one.
DOCUMENTED_MINIMUM = {
    "notification_type": "MEDIA_PENDING",
    "subject": "The End of Oak Street (2026)",
    "media": {"media_type": "movie", "tmdbId": "603"},
    "request": {
        "request_id": "99",
        "requestedBy_username": "Robin Example",
        "requestedBy_email": "robin@example.com",
    },
}

#: The same five fields flattened, which the README offers as the easier thing
#: to merge into a template built for something else.
DOCUMENTED_FLAT = {
    "notification_type": "MEDIA_PENDING",
    "subject": "The End of Oak Street (2026)",
    "media_type": "movie",
    "media_tmdbid": "603",
    "request_id": "99",
    "requestedBy_username": "Robin Example",
    "requestedBy_email": "robin@example.com",
}


@pytest.mark.parametrize(
    "payload", [DOCUMENTED_MINIMUM, DOCUMENTED_FLAT], ids=["nested", "flat"]
)
def test_the_documented_minimum_payload_reads(payload: dict[str, Any]) -> None:
    """Both shapes the README prints, read the same way."""
    event = parse_event(payload)

    assert event.is_request_filed
    assert event.request_id == 99
    assert event.tmdb_id == 603
    assert event.username == "Robin Example"
    assert event.email == "robin@example.com"
    assert event.media_type == "movie"
    assert event.subject == "The End of Oak Street (2026)"


def test_keys_the_template_carries_for_something_else_are_ignored() -> None:
    """An ntfy- or Discord-shaped template only has to *keep* what this reads."""
    event = parse_event(
        {**DOCUMENTED_MINIMUM, "topic": "media", "priority": 4, "tags": ["clapper"]}
    )

    assert event.is_request_filed
    assert event.request_id == 99


def test_an_unsubstituted_variable_is_not_a_username() -> None:
    """Seerr leaves a name it does not recognise in the body verbatim."""
    event = parse_event(
        webhook_payload(username="{{requestedBy_username}}", request_id="{{request_id}}")
    )

    assert event.username is None
    assert event.request_id is None


def test_a_payload_with_nothing_in_it_is_inert_rather_than_an_error() -> None:
    event = parse_event({})

    assert event.notification_type == ""
    assert not event.is_request_filed
    assert not event.is_test
    assert event.request_id is None


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("The End of Oak Street (2026)", ("The End of Oak Street", 2026)),
        ("Severance", ("Severance", None)),
        ("2001: A Space Odyssey", ("2001: A Space Odyssey", None)),
        ("(1999)", ("(1999)", None)),
    ],
)
def test_split_year(subject: str, expected: tuple[str, int | None]) -> None:
    assert split_year(subject) == expected


# --------------------------------------------------------------------------- #
# Getting in
# --------------------------------------------------------------------------- #


async def test_the_endpoint_refuses_everyone_until_a_secret_is_generated(
    client: httpx.AsyncClient, configured: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """No secret is not "open" — it is off."""
    with caplog.at_level("WARNING"):
        response = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert response.status_code == 503
    assert "Configuration tab" in response.json()["detail"]
    assert "no secret is configured" in caplog.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "wrong"}, {"Authorization": f"Bearer {SECRET}x"}],
    ids=["missing", "wrong", "wrong-bearer"],
)
async def test_a_bad_secret_is_rejected(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    headers: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await webhook_enabled(db, configured)

    with caplog.at_level("WARNING"):
        response = await client.post(WEBHOOK, headers=headers, json=webhook_payload())

    assert response.status_code == 401
    assert await request_rows(db) == []

    # The response says only "Rejected"; the reason belongs in the log, where
    # the admin can see it and the caller cannot. Without it, a refused
    # delivery and one that never arrived look identical from Seerr's side.
    assert "refused a Seerr webhook delivery" in caplog.text
    expected = "was missing" if not headers else "did not match"
    assert expected in caplog.text


async def test_a_bearer_prefixed_secret_is_accepted(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Not what Seerr sends, but what an admin who has done this before types."""
    await webhook_enabled(db, configured)

    response = await client.post(
        WEBHOOK, headers={"Authorization": f"Bearer {SECRET}"}, json=webhook_payload()
    )

    assert response.status_code == 200
    assert response.json() == {"handled": True}


async def test_a_body_that_is_not_json_is_a_400(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await webhook_enabled(db, configured)

    with caplog.at_level("WARNING"):
        response = await client.post(WEBHOOK, headers=auth(), content=b"not json")

    assert response.status_code == 400
    assert "not valid JSON" in caplog.text


# --------------------------------------------------------------------------- #
# What it does with a delivery
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_request_filed_in_seerr_is_logged_and_pushed(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The whole point: someone requests in Seerr, the admin's phone says so."""
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)
    db.add(User(seerr_user_id=7, plex_username="Robin Example"))
    await db.commit()

    response = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert response.status_code == 200
    assert response.json() == {"handled": True}

    assert lines_of(relay) == {
        "title": "The End of Oak Street (2026)",
        "subtitle": "Requested by Robin Example",
    }

    rows = await request_rows(db)
    assert len(rows) == 1
    requester = (
        await db.execute(select(User).where(User.plex_username == "Robin Example"))
    ).scalar_one()
    assert rows[0].user_id == requester.id
    assert rows[0].detail == {
        "kind": "request",
        "source": "seerr",
        "success": True,
        "seerr_request_id": 99,
        "tmdb_id": 603,
        "type": "movie",
        "requested_by": "Robin Example",
        "seerr_event": "MEDIA_PENDING",
    }


@respx.mock
async def test_a_requester_with_no_local_row_is_still_logged_and_pushed(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Someone who has never opened the app has no ``users`` row to attribute to.

    Unattributed, not dropped: the admin still needs to know a request is
    waiting, and the name Seerr sent is carried on the row so the activity page
    is not reduced to an em dash.
    """
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    response = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert response.status_code == 200
    assert lines_of(relay)["subtitle"] == "Requested by Robin Example"

    rows = await request_rows(db)
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].detail["requested_by"] == "Robin Example"
    assert rows[0].detail["unmatched_user"] is True


@respx.mock
async def test_a_requester_is_matched_by_email_too(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """``plex_username`` holds whichever name Seerr gave us at sign-in.

    For a user with no Plex username that is their email, so an email match is
    not a fallback for tidiness — it is the only thing that matches them.
    """
    mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)
    db.add(User(seerr_user_id=7, plex_username="robin@example.com"))
    await db.commit()

    await client.post(
        WEBHOOK, headers=auth(), json=webhook_payload(username="Someone Else")
    )

    rows = await request_rows(db)
    assert rows[0].user_id is not None
    assert "unmatched_user" not in rows[0].detail


@respx.mock
async def test_the_requester_is_not_notified_about_their_own_request(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The admin requesting in Seerr's own UI is the ordinary way this happens."""
    relay = mock_relay()
    admin = await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    response = await client.post(
        WEBHOOK, headers=auth(), json=webhook_payload(username=admin.plex_username)
    )

    assert response.status_code == 200
    assert not relay.called
    assert (await request_rows(db))[0].user_id == admin.id


@respx.mock
async def test_a_test_notification_is_acknowledged_and_nothing_else(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Seerr's Test button proves the URL and the secret. That is all it is for."""
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    response = await client.post(
        WEBHOOK,
        headers=auth(),
        json={
            "notification_type": "TEST_NOTIFICATION",
            "subject": "Test Notification",
            "message": "Check check, 1, 2, 3.",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"handled": False, "reason": "test"}
    assert not relay.called
    assert await request_rows(db) == []


@respx.mock
async def test_an_event_that_is_not_a_new_request_is_accepted_and_ignored(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """200, so Seerr does not log a failure and retry something we ignore on purpose."""
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    response = await client.post(
        WEBHOOK, headers=auth(), json=webhook_payload(notification_type="MEDIA_AVAILABLE")
    )

    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert not relay.called
    assert await request_rows(db) == []


@respx.mock
async def test_a_redelivered_webhook_is_announced_once(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Seerr retries a delivery it thinks failed. The admin should not hear twice."""
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    first = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())
    second = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert first.json() == {"handled": True}
    assert second.json() == {"handled": False, "reason": "already known"}
    assert len(relay.calls) == 1
    assert len(await request_rows(db)) == 1


@respx.mock
async def test_a_request_filed_through_the_app_is_not_announced_twice(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    plex_headers: dict,
) -> None:
    """The echo this whole dedup exists for.

    ``POST /request`` files a request with Seerr, logs it and pushes. Seerr then
    tells its webhook subscribers about the request it just accepted — including
    us, since it has no idea the request came through here in the first place.
    """
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json=seerr_user_payload(user_id=7, username="Robin Example")
        )
    )
    respx.post(f"{SEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99, "status": 1})
    )
    relay = mock_relay()

    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)
    assert (await client.get("/register", headers=plex_headers)).status_code == 200

    requester = (
        await db.execute(select(User).where(User.plex_username == "Robin Example"))
    ).scalar_one()
    action = (
        await db.execute(select(Action).where(Action.is_system.is_(True)))
    ).scalar_one()
    await grant(db, requester, action)

    filed = await client.post(
        "/request",
        headers=plex_headers,
        json={
            "tmdb_id": 603,
            "type": "movie",
            "media_title": "The End of Oak Street",
            "media_year": 2026,
        },
    )
    assert filed.status_code == 200

    echo = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert echo.json() == {"handled": False, "reason": "already known"}
    assert len(relay.calls) == 1
    assert len(await request_rows(db)) == 1
    assert await db.get(SeerrRequestNotice, 99) is not None


@respx.mock
async def test_a_payload_with_no_notification_type_says_so_in_the_log(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one template mistake nothing else would reveal.

    Every delivery falls through as ignored while Seerr reports success, so an
    admin sees a working webhook and no notifications. This log line is the
    only thing standing between that and an afternoon.
    """
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    with caplog.at_level("WARNING"):
        response = await client.post(
            WEBHOOK, headers=auth(), json=webhook_payload(notification_type="")
        )

    assert response.json() == {"handled": False, "reason": "no notification_type"}
    assert not relay.called
    assert await request_rows(db) == []
    assert "notification_type" in caplog.text


@respx.mock
async def test_a_request_with_no_id_is_announced_but_warned_about(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Announced, because the admin still needs to know someone asked.

    Warned about, because with no id to claim it by there is nothing to tell
    this delivery apart from a redelivery of itself — so it is announced again
    every time Seerr retries.
    """
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    with caplog.at_level("WARNING"):
        first = await client.post(
            WEBHOOK, headers=auth(), json=webhook_payload(request_id=None)
        )
        await client.post(WEBHOOK, headers=auth(), json=webhook_payload(request_id=None))

    assert first.json() == {"handled": True}
    assert "request_id" in caplog.text
    # The duplicate this warns about, demonstrated rather than asserted about.
    assert len(relay.calls) == 2
    assert len(await request_rows(db)) == 2


@respx.mock
async def test_an_ignored_event_does_not_warn(
    client: httpx.AsyncClient,
    db: AsyncSession,
    configured: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ticking every type in Seerr is allowed, so it must not fill the log."""
    await webhook_enabled(db, configured)

    with caplog.at_level("WARNING"):
        await client.post(
            WEBHOOK,
            headers=auth(),
            json=webhook_payload(notification_type="MEDIA_AVAILABLE"),
        )

    assert caplog.text == ""


@respx.mock
async def test_a_payload_with_no_subject_falls_back_to_the_tmdb_id(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """Same last resort ``POST /request`` has: ugly, and better than "Unknown"."""
    relay = mock_relay()
    await an_admin_with_a_device(db, configured)
    await webhook_enabled(db, configured)

    await client.post(WEBHOOK, headers=auth(), json=webhook_payload(subject=None))

    assert lines_of(relay)["title"] == "TMDB 603"


@respx.mock
async def test_a_request_is_logged_even_with_notifications_switched_off(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    """The log and the push are separate promises; only one needs the relay."""
    relay = mock_relay()
    await webhook_enabled(db, configured)

    response = await client.post(WEBHOOK, headers=auth(), json=webhook_payload())

    assert response.json() == {"handled": True}
    assert not relay.called
    assert len(await request_rows(db)) == 1
