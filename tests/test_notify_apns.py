"""The APNs client: signing, the push itself, and reading Apple's answers.

Apple's endpoint is mocked, but nothing between here and it is: the real JWT is
minted and verified against the key that signed it, and the real headers are
asserted, because those are the two things that fail silently in production.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

from cplus_service.db.models import ApnsEnvironment, Config
from cplus_service.notify.apns import (
    PROVIDER_TOKEN_LIFETIME_SECONDS,
    ApnsClient,
    ApnsConfigError,
    ApnsSettings,
    ProviderTokenCache,
    SendOutcome,
    build_payload,
    sign_provider_token,
    validate_private_key,
)
from cplus_service.notify.messages import MediaSummary, user_requested

from .conftest import (
    APNS_BUNDLE_ID,
    APNS_KEY_ID,
    APNS_PRODUCTION,
    APNS_SANDBOX,
    APNS_TEAM_ID,
)

DEVICE_TOKEN = "ab" * 32
NOTIFICATION = user_requested(
    MediaSummary(title="The End of Oak Street", year=2026), username="Jane Dietrich"
)


def _decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


# --------------------------------------------------------------------------- #
# Provider tokens
# --------------------------------------------------------------------------- #


def test_a_provider_token_verifies_against_the_key_that_signed_it(
    apns_settings: ApnsSettings, apns_key_pem: str
) -> None:
    """The end-to-end check the DER/raw signature trap would fail."""
    token = sign_provider_token(apns_settings, issued_at=1_700_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")

    assert _decode_segment(header_b64) == {"alg": "ES256", "kid": APNS_KEY_ID}
    assert _decode_segment(claims_b64) == {"iss": APNS_TEAM_ID, "iat": 1_700_000_000}

    raw = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    assert len(raw) == 64, "ES256 must be a fixed-width r||s pair, not DER"

    public_key = serialization.load_pem_private_key(
        apns_key_pem.encode(), password=None
    ).public_key()
    der = asym_utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    public_key.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_a_provider_token_does_not_verify_against_a_different_key(
    apns_settings: ApnsSettings,
) -> None:
    """Guards the check above from passing on a signature it never looked at."""
    token = sign_provider_token(apns_settings, issued_at=1_700_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")
    raw = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    der = asym_utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )

    stranger = ec.generate_private_key(ec.SECP256R1()).public_key()
    with pytest.raises(InvalidSignature):
        stranger.verify(
            der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256())
        )


def test_a_cached_token_is_reused_until_it_ages_out(
    apns_settings: ApnsSettings,
) -> None:
    """Apple refuses to mint these faster than one per 20 minutes."""
    cache = ProviderTokenCache()

    first = cache.get(apns_settings, now=1_000.0)
    assert cache.get(apns_settings, now=1_000.0 + 60) == first

    later = cache.get(apns_settings, now=1_000.0 + PROVIDER_TOKEN_LIFETIME_SECONDS + 1)
    assert later != first


def test_changing_the_key_takes_effect_on_the_next_push(
    apns_settings: ApnsSettings,
) -> None:
    """Keyed by credential, so a key swap is not stuck behind the old timer."""
    cache = ProviderTokenCache()
    first = cache.get(apns_settings, now=1_000.0)

    rekeyed = ApnsSettings(
        team_id=apns_settings.team_id,
        key_id="OTHERKEY99",
        bundle_id=apns_settings.bundle_id,
        private_key_pem=apns_settings.private_key_pem,
    )
    assert cache.get(rekeyed, now=1_000.0) != first


def test_invalidating_forces_a_fresh_token(apns_settings: ApnsSettings) -> None:
    cache = ProviderTokenCache()
    first = cache.get(apns_settings, now=1_000.0)
    cache.invalidate(apns_settings)
    assert cache.get(apns_settings, now=1_000.5) != first


# --------------------------------------------------------------------------- #
# Settings and key validation
# --------------------------------------------------------------------------- #


def test_push_is_off_until_every_field_is_set(apns_key_pem: str) -> None:
    """Three fields out of four cannot send anything, so they do not count."""
    config = Config(id=1, apns_team_id=APNS_TEAM_ID, apns_key_id=APNS_KEY_ID)
    assert ApnsSettings.from_config(config) is None

    config.apns_bundle_id = APNS_BUNDLE_ID
    assert ApnsSettings.from_config(config) is None

    config.apns_private_key = apns_key_pem
    settings = ApnsSettings.from_config(config)
    assert settings is not None
    assert settings.bundle_id == APNS_BUNDLE_ID


def test_whitespace_only_fields_do_not_count_as_configured(apns_key_pem: str) -> None:
    config = Config(
        id=1,
        apns_team_id="   ",
        apns_key_id=APNS_KEY_ID,
        apns_bundle_id=APNS_BUNDLE_ID,
        apns_private_key=apns_key_pem,
    )
    assert ApnsSettings.from_config(config) is None


def test_a_valid_key_passes_validation(apns_key_pem: str) -> None:
    validate_private_key(apns_key_pem)


def test_a_key_that_is_not_a_key_is_refused_with_an_actionable_message() -> None:
    with pytest.raises(ApnsConfigError, match="BEGIN and END"):
        validate_private_key("not a key at all")


def test_an_rsa_key_is_refused_rather_than_failing_at_the_first_push() -> None:
    """Apple's push keys are ES256; anything else is a wrong file, not a wrong password."""
    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ApnsConfigError, match="elliptic-curve"):
        validate_private_key(pem)


# --------------------------------------------------------------------------- #
# The payload
# --------------------------------------------------------------------------- #


def test_the_payload_carries_title_and_subtitle_and_no_body() -> None:
    payload = build_payload(NOTIFICATION)
    assert payload["aps"]["alert"] == {
        "title": "The End of Oak Street (2026)",
        "subtitle": "Requested by Jane Dietrich",
    }
    assert "body" not in payload["aps"]["alert"]


def test_payload_data_rides_alongside_aps_rather_than_inside_it() -> None:
    payload = build_payload(
        user_requested(MediaSummary(title="X"), username="Y", tmdb_id=7)
    )
    assert payload["cplus"] == {"type": "user_requested", "tmdb_id": 7}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


@pytest.fixture
def client_factory(apns_settings: ApnsSettings):
    """An :class:`ApnsClient` over a real httpx client, for respx to intercept."""

    def build(http: httpx.AsyncClient) -> ApnsClient:
        return ApnsClient(apns_settings, client=http, tokens=ProviderTokenCache())

    return build


@respx.mock
async def test_a_push_carries_the_headers_apple_requires(client_factory) -> None:
    route = respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(200, headers={"apns-id": "abc-123"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.DELIVERED
    assert result.apns_id == "abc-123"

    sent = route.calls.last.request
    assert sent.headers["apns-topic"] == APNS_BUNDLE_ID
    assert sent.headers["apns-push-type"] == "alert"
    assert sent.headers["apns-priority"] == "10"
    assert sent.headers["authorization"].startswith("bearer ey")
    assert json.loads(sent.content)["aps"]["alert"]["title"] == (
        "The End of Oak Street (2026)"
    )


@respx.mock
async def test_a_sandbox_device_goes_to_the_sandbox_host(client_factory) -> None:
    """A development token is meaningless to the production host."""
    route = respx.post(f"{APNS_SANDBOX}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(200)
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION,
            device_token=DEVICE_TOKEN,
            environment=ApnsEnvironment.SANDBOX,
        )

    assert result.delivered
    assert route.called


@respx.mock
async def test_a_410_reports_the_token_as_dead(client_factory) -> None:
    respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(410, json={"reason": "Unregistered"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.UNREGISTERED
    assert result.reason == "Unregistered"


@respx.mock
async def test_a_bad_device_token_is_dead_too_despite_its_400(client_factory) -> None:
    """The common cause is a sandbox token sent to production. It never recovers."""
    respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(400, json={"reason": "BadDeviceToken"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.UNREGISTERED


@respx.mock
async def test_a_bad_topic_is_a_failure_not_a_dead_token(client_factory) -> None:
    """The bundle id is wrong for every device, so deleting one would fix nothing."""
    respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(400, json={"reason": "BadTopic"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.FAILED
    assert result.reason == "BadTopic"


@respx.mock
async def test_a_throttled_push_is_retried_once_and_can_succeed(
    client_factory,
) -> None:
    route = respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        side_effect=[
            httpx.Response(429, json={"reason": "TooManyRequests"}),
            httpx.Response(200),
        ]
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.delivered
    assert route.call_count == 2


@respx.mock
async def test_an_expired_provider_token_is_re_minted_and_retried(
    apns_settings: ApnsSettings,
) -> None:
    """The one 403 worth retrying — and only after throwing the cached token away."""
    route = respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        side_effect=[
            httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
            httpx.Response(200),
        ]
    )

    tokens = ProviderTokenCache()
    async with httpx.AsyncClient() as http:
        client = ApnsClient(apns_settings, client=http, tokens=tokens)
        result = await client.send(NOTIFICATION, device_token=DEVICE_TOKEN)

    assert result.delivered
    assert route.call_count == 2
    first, second = (call.request.headers["authorization"] for call in route.calls)
    assert first != second, "the stale token must not be presented twice"


@respx.mock
async def test_a_bad_provider_token_is_not_retried(client_factory) -> None:
    """Signed wrong, not signed stale — retrying presents the same bad token."""
    route = respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(403, json={"reason": "InvalidProviderToken"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.FAILED
    assert route.call_count == 1


@respx.mock
async def test_a_persistent_server_error_gives_up_after_one_retry(
    client_factory,
) -> None:
    route = respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(503, json={"reason": "ServiceUnavailable"})
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.FAILED
    assert route.call_count == 2


@respx.mock
async def test_a_transport_failure_is_a_failure_not_an_exception(
    client_factory,
) -> None:
    """Nothing is waiting on a push; it must not propagate out of the client."""
    respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.FAILED
    assert "no route to host" in (result.reason or "")


@respx.mock
async def test_a_non_json_error_body_still_yields_an_outcome(client_factory) -> None:
    """A proxy in front of Apple can answer with HTML. That is still a failure."""
    respx.post(f"{APNS_PRODUCTION}/3/device/{DEVICE_TOKEN}").mock(
        return_value=httpx.Response(502, text="<html>bad gateway</html>")
    )

    async with httpx.AsyncClient() as http:
        result = await client_factory(http).send(
            NOTIFICATION, device_token=DEVICE_TOKEN
        )

    assert result.outcome is SendOutcome.FAILED
    assert result.reason is None
