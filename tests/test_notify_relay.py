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
    EnrollmentError,
    RelayClient,
    RelaySettings,
    SendOutcome,
    build_request,
    enrol,
    relay_base_url,
)

from .conftest import (
    RELAY_API_KEY,
    RELAY_ENROL_URL,
    RELAY_INSTANCE_ID,
    RELAY_PUSH_URL,
    RELAY_URL,
    enrolled,
)

NOTIFICATION = user_requested(
    MediaSummary(title="The End of Oak Street", year=2026), username="Robin Example"
)
DEVICE_TOKEN = "aa" * 16


def config_with(**kwargs) -> Config:
    """A config row with notifications on and the relay set, unless overridden."""
    defaults = {
        "id": 1,
        "notifications_enabled": True,
        "notification_relay_instance_id": RELAY_INSTANCE_ID,
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
    """Only reachable when enrolling failed, since enabling is what enrols."""
    assert RelaySettings.from_config(config_with(notification_relay_api_key=None)) is None


def test_the_url_comes_from_the_environment_not_the_config_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody runs their own relay, so it stopped being an admin setting.

    The environment override is for development and for a fork with its own
    Apple account — the only two cases that were ever real.
    """
    monkeypatch.delenv("CPLUS_RELAY_URL", raising=False)
    assert relay_base_url() == DEFAULT_RELAY_URL

    monkeypatch.setenv("CPLUS_RELAY_URL", "https://elsewhere.test/")
    assert relay_base_url() == "https://elsewhere.test"


def test_a_trailing_slash_does_not_double_up_in_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CPLUS_RELAY_URL", f"{RELAY_URL}/")
    settings = RelaySettings.from_config(config_with())

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
# Enrolling
# --------------------------------------------------------------------------- #


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@respx.mock
async def test_enrolling_returns_an_identity(http: httpx.AsyncClient) -> None:
    """The whole of setup: one POST, no admin input, no credential handling."""
    route = respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(201, json=enrolled())
    )

    result = await enrol(client=http)

    assert result.instance_id == RELAY_INSTANCE_ID
    assert result.api_key == RELAY_API_KEY
    assert result.ready is True
    # Unauthenticated and bodiless: there is nothing we could tell the relay
    # that it could verify, so it is not asked.
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_a_200_is_accepted_as_well_as_a_201(http: httpx.AsyncClient) -> None:
    """Not worth failing setup over which success code the relay chose."""
    respx.post(RELAY_ENROL_URL).mock(return_value=httpx.Response(200, json=enrolled()))

    assert (await enrol(client=http)).api_key == RELAY_API_KEY


@respx.mock
async def test_an_unready_relay_still_enrols_but_says_so(
    http: httpx.AsyncClient,
) -> None:
    """The key is good; the relay simply has nothing behind it yet."""
    respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(201, json=enrolled(ready=False))
    )

    result = await enrol(client=http)

    assert result.api_key == RELAY_API_KEY
    assert result.ready is False


@respx.mock
async def test_an_unreachable_relay_is_an_actionable_error(
    http: httpx.AsyncClient,
) -> None:
    respx.post(RELAY_ENROL_URL).mock(side_effect=httpx.ConnectError("dns"))

    with pytest.raises(EnrollmentError, match="Could not reach"):
        await enrol(client=http)


@respx.mock
async def test_enrollment_being_closed_says_it_is_not_ours_to_fix(
    http: httpx.AsyncClient,
) -> None:
    respx.post(RELAY_ENROL_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(EnrollmentError, match="not issuing new keys"):
        await enrol(client=http)


@respx.mock
async def test_being_rate_limited_says_to_wait(http: httpx.AsyncClient) -> None:
    respx.post(RELAY_ENROL_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(EnrollmentError, match="rate-limiting"):
        await enrol(client=http)


@respx.mock
async def test_a_success_we_cannot_use_is_an_error(http: httpx.AsyncClient) -> None:
    """Worse than a failure, because everything downstream would carry on as
    though setup had worked."""
    respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(201, json={"instance_id": "x"})
    )

    with pytest.raises(EnrollmentError, match="did not understand"):
        await enrol(client=http)


@respx.mock
async def test_an_error_body_is_quoted_back(http: httpx.AsyncClient) -> None:
    respx.post(RELAY_ENROL_URL).mock(
        return_value=httpx.Response(500, json={"detail": "the relay is unwell"})
    )

    with pytest.raises(EnrollmentError, match="the relay is unwell"):
        await enrol(client=http)
