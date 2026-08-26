"""Talking to the forwarding relay.

Unit tests for :mod:`cplus_service.notify.relay` alone — no database, no app.
The theme running through them: the relay's HTTP status and Apple's verdict are
two different facts, and reading one as the other is the mistake this module
exists to not make. A 401 from the relay says nothing about a device token; a
200 carrying ``"result": "unregistered"`` says everything about one.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cplus_service.db.models import ApnsEnvironment, Config
from cplus_service.notify.messages import MediaSummary, user_requested
from cplus_service.notify.relay import (
    DEFAULT_RELAY_URL,
    RelayClient,
    RelaySettings,
    SendOutcome,
    build_request,
    verify,
)

from .conftest import RELAY_API_KEY, RELAY_PUSH_URL, RELAY_URL, RELAY_VERIFY_URL

NOTIFICATION = user_requested(
    MediaSummary(title="The End of Oak Street", year=2026), username="Robin Example"
)
DEVICE_TOKEN = "aa" * 16


def config_with(**kwargs) -> Config:
    """A config row with notifications on and the relay set, unless overridden."""
    defaults = {
        "id": 1,
        "notifications_enabled": True,
        "notification_relay_url": RELAY_URL,
        "notification_relay_api_key": RELAY_API_KEY,
    }
    return Config(**{**defaults, **kwargs})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_settings_are_read_from_a_configured_row() -> None:
    settings = RelaySettings.from_config(config_with())

    assert settings is not None
    assert settings.url == RELAY_URL
    assert settings.api_key == RELAY_API_KEY


def test_the_master_switch_being_off_means_no_settings() -> None:
    """Even with a key saved. The switch is consent, not a preference."""
    assert RelaySettings.from_config(config_with(notifications_enabled=False)) is None


def test_no_api_key_means_no_settings() -> None:
    assert RelaySettings.from_config(config_with(notification_relay_api_key=None)) is None


def test_an_unset_url_falls_back_to_the_default() -> None:
    """So an install that only ever pasted a key still points somewhere."""
    settings = RelaySettings.from_config(config_with(notification_relay_url=None))

    assert settings is not None
    assert settings.url == DEFAULT_RELAY_URL


def test_a_trailing_slash_does_not_double_up_in_the_path() -> None:
    settings = RelaySettings.from_config(
        config_with(notification_relay_url=f"{RELAY_URL}/")
    )

    assert settings is not None
    assert settings.endpoint("/v1/push") == RELAY_PUSH_URL


# --------------------------------------------------------------------------- #
# The request body
# --------------------------------------------------------------------------- #


def test_the_request_is_text_not_an_apns_payload() -> None:
    """The relay builds `aps` itself and refuses one sent from here.

    That is what stops any instance sending a silent `content-available`
    background wake signed with the relay operator's key.
    """
    body = build_request(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert "aps" not in body
    assert body["title"] == "The End of Oak Street (2026)"
    assert body["subtitle"] == "Requested by Robin Example"


def test_the_environment_travels_with_the_device() -> None:
    """Only this side knows which build a token came from."""
    body = build_request(
        NOTIFICATION, device_token=DEVICE_TOKEN, environment=ApnsEnvironment.SANDBOX
    )

    assert body["environment"] == "sandbox"


def test_a_collapse_id_is_omitted_unless_given() -> None:
    assert "collapse_id" not in build_request(NOTIFICATION, device_token=DEVICE_TOKEN)
    assert (
        build_request(NOTIFICATION, device_token=DEVICE_TOKEN, collapse_id="req-7")[
            "collapse_id"
        ]
        == "req-7"
    )


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


@pytest.fixture
async def sender(relay_settings: RelaySettings):
    async with httpx.AsyncClient() as http:
        yield RelayClient(relay_settings, client=http)


@respx.mock
async def test_a_delivered_push_is_reported_as_delivered(sender: RelayClient) -> None:
    route = respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(
            200, json={"result": "delivered", "apns_id": "abc-123"}
        )
    )

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.delivered
    assert result.apns_id == "abc-123"
    assert route.calls[0].request.headers["authorization"] == f"Bearer {RELAY_API_KEY}"


@respx.mock
async def test_a_dead_token_is_reported_as_unregistered(sender: RelayClient) -> None:
    """A 200 from the relay carrying Apple's 'this token is gone'."""
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(
            200, json={"result": "unregistered", "reason": "Unregistered"}
        )
    )

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.UNREGISTERED
    assert result.reason == "Unregistered"


@respx.mock
async def test_apple_refusing_comes_back_as_a_failure(sender: RelayClient) -> None:
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "failed", "reason": "BadTopic"})
    )

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.FAILED
    assert result.reason == "BadTopic"


@pytest.mark.parametrize("status_code", [401, 429, 503])
@respx.mock
async def test_the_relays_own_errors_never_look_like_a_dead_token(
    sender: RelayClient, status_code: int
) -> None:
    """The distinction the whole module turns on.

    A key that was revoked, a rate limit, a relay with no signing key: none of
    them is a verdict on the device token, and treating one as such would
    delete every registered device the next time the relay had a bad afternoon.
    """
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(status_code, json={"detail": "nope"})
    )

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.FAILED
    assert result.status_code == status_code
    assert result.reason == "nope"


@respx.mock
async def test_an_unreachable_relay_is_a_failure_not_an_exception(
    sender: RelayClient,
) -> None:
    """Sending runs on a background task; nothing is left to catch a raise."""
    respx.post(RELAY_PUSH_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.FAILED
    assert "no route to host" in (result.reason or "")


@respx.mock
async def test_an_unrecognised_result_is_a_failure(sender: RelayClient) -> None:
    respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "maybe"})
    )

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.FAILED
    assert "unrecognised" in (result.reason or "")


@respx.mock
async def test_a_200_that_is_not_json_is_a_failure(sender: RelayClient) -> None:
    respx.post(RELAY_PUSH_URL).mock(return_value=httpx.Response(200, text="<html>"))

    result = await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.outcome is SendOutcome.FAILED


@respx.mock
async def test_a_push_is_not_retried_from_this_side(sender: RelayClient) -> None:
    """The relay already retries what is worth retrying against Apple.

    Retrying again here would double a burst the relay is rate-limiting us for.
    """
    route = respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(429, json={"detail": "slow down"})
    )

    await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert route.call_count == 1


@respx.mock
async def test_the_notifications_data_is_forwarded_for_the_app_to_route_on(
    sender: RelayClient,
) -> None:
    route = respx.post(RELAY_PUSH_URL).mock(
        return_value=httpx.Response(200, json={"result": "delivered"})
    )

    await sender.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert json.loads(route.calls[0].request.read())["data"] == NOTIFICATION.data


# --------------------------------------------------------------------------- #
# Verifying a key
# --------------------------------------------------------------------------- #


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@respx.mock
async def test_verify_reports_the_instance_and_topic_on_success(
    relay_settings: RelaySettings, http: httpx.AsyncClient
) -> None:
    respx.get(RELAY_VERIFY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "instance": "notcanopy",
                "bundle_id": "com.example.cplus",
                "ready": True,
                "rate_limit_per_minute": 120,
            },
        )
    )

    result = await verify(relay_settings, client=http)

    assert result.ok
    assert "notcanopy" in result.message
    assert "com.example.cplus" in result.message


@respx.mock
async def test_verify_says_plainly_when_the_key_is_wrong(
    relay_settings: RelaySettings, http: httpx.AsyncClient
) -> None:
    respx.get(RELAY_VERIFY_URL).mock(return_value=httpx.Response(401))

    result = await verify(relay_settings, client=http)

    assert not result.ok
    assert "does not recognise this API key" in result.message


@respx.mock
async def test_verify_separates_a_good_key_from_an_unready_relay(
    relay_settings: RelaySettings, http: httpx.AsyncClient
) -> None:
    """Different failure, different owner. An admin re-pasting a key that was
    already fine is the outcome this distinction exists to prevent."""
    respx.get(RELAY_VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "instance": "notcanopy", "ready": False}
        )
    )

    result = await verify(relay_settings, client=http)

    assert not result.ok
    assert "Nothing is wrong on this end" in result.message


@respx.mock
async def test_verify_reports_an_unreachable_relay_as_such(
    relay_settings: RelaySettings, http: httpx.AsyncClient
) -> None:
    respx.get(RELAY_VERIFY_URL).mock(side_effect=httpx.ConnectError("dns"))

    result = await verify(relay_settings, client=http)

    assert not result.ok
    assert "Could not reach the relay" in result.message
