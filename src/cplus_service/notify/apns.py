"""Token-based APNs delivery.

Provider *tokens*, not certificates: a single ``.p8`` signing key plus a team
id and a key id, from which we mint a short-lived ES256 JWT and send it as a
bearer token on every push.  That is Apple's current scheme and the reason
nothing here deals in client certificates or a TLS keypair — the credential is
a signing key, and it is the same one for every app on the team.

Three things about APNs shape this module:

* **It is HTTP/2 only.**  A plain HTTP/1.1 client gets a protocol error, not a
  helpful message, so the client used here is created with ``http2=True`` and
  kept apart from the service's other outbound clients for that reason alone.
* **The provider token is reusable and rate-limited.**  Apple refuses a token
  minted more than once in 20 minutes and rejects one older than an hour, so it
  must be cached across pushes rather than signed per request — see
  :class:`ProviderTokenCache`.
* **A 410 is a fact, not an error.**  It means the app is gone from that device
  and the token will never work again; the only correct response is to delete
  it.  That is reported as its own outcome rather than folded in with failures,
  because the caller has to act on it.

Nothing here reads configuration or touches the database.  It is handed
settings and a device token and reports what happened;
:mod:`cplus_service.notify.service` is what decides who gets sent what and
what to do about a dead token.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

from ..db.models import ApnsEnvironment, Config
from .messages import Notification

logger = logging.getLogger(__name__)

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"

#: Apple rejects a provider token older than one hour and refuses to mint a new
#: one more often than every 20 minutes.  Renewing at 45 leaves room on both
#: sides: comfortably inside the hour, comfortably past the 20-minute floor.
PROVIDER_TOKEN_LIFETIME_SECONDS = 45 * 60

#: Reasons that mean "this device token is dead, stop using it".  Apple returns
#: ``Unregistered`` with 410 for an uninstalled app and ``BadDeviceToken`` with
#: 400 for one that was never valid for this topic (commonly a sandbox token
#: sent to production).  Both are permanent for the token as stored.
DEAD_TOKEN_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"})

#: Statuses worth one more attempt.  429 is Apple throttling this token, 500
#: and 503 are Apple having a bad moment; everything else is a request we got
#: wrong and would get wrong again.
RETRYABLE_STATUSES = frozenset({429, 500, 503})

#: Signed with a stale provider token — the one 403 that is worth retrying,
#: after minting a fresh one.
EXPIRED_TOKEN_REASON = "ExpiredProviderToken"


class ApnsConfigError(Exception):
    """The stored APNs credentials are missing or unusable.

    Raised when the ``.p8`` will not parse or is not an elliptic-curve key —
    an admin pasting the wrong file is the expected cause, so the message is
    written to be read by one.
    """


@dataclass(frozen=True)
class ApnsSettings:
    """Everything needed to sign and address a push.

    Built from the config row via :meth:`from_config`, which returns ``None``
    when push is simply not set up yet.  That is the ordinary state of a fresh
    install — and of this one until the signing key arrives — so it is a
    ``None``, not an exception.
    """

    team_id: str
    key_id: str
    bundle_id: str
    private_key_pem: str

    @classmethod
    def from_config(cls, config: Config) -> ApnsSettings | None:
        """Read the four required fields, or ``None`` if any is unset.

        All-or-nothing on purpose: three fields out of four cannot send
        anything, and treating that as "configured" would turn a half-finished
        settings page into a stream of delivery failures.
        """
        team_id = (config.apns_team_id or "").strip()
        key_id = (config.apns_key_id or "").strip()
        bundle_id = (config.apns_bundle_id or "").strip()
        private_key_pem = (config.apns_private_key or "").strip()

        if not (team_id and key_id and bundle_id and private_key_pem):
            return None

        return cls(
            team_id=team_id,
            key_id=key_id,
            bundle_id=bundle_id,
            private_key_pem=private_key_pem,
        )


class SendOutcome(StrEnum):
    """What became of one push."""

    DELIVERED = "delivered"
    UNREGISTERED = "unregistered"
    """Apple says this device token is dead. Delete it."""

    FAILED = "failed"


@dataclass(frozen=True)
class SendResult:
    """The outcome of one push, with enough detail to log it usefully."""

    outcome: SendOutcome
    status_code: int | None = None
    reason: str | None = None
    """Apple's own machine-readable reason string, when it gave one."""

    apns_id: str | None = None
    """Apple's id for the push, for correlating with their delivery console."""

    @property
    def delivered(self) -> bool:
        return self.outcome is SendOutcome.DELIVERED


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """Parse a ``.p8`` into a signing key, with a message an admin can act on."""
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ApnsConfigError(
            "The APNs key could not be read. Paste the contents of the .p8 file "
            "exactly as downloaded, including the BEGIN and END lines."
        ) from exc

    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ApnsConfigError(
            "The APNs key is not an elliptic-curve key. Apple's push keys are "
            "ES256 .p8 files — this looks like a different kind of key."
        )
    return key


def sign_provider_token(settings: ApnsSettings, *, issued_at: int | None = None) -> str:
    """Mint one ES256 provider token (a JWT) for ``settings``.

    Hand-rolled rather than pulled from a JWT library because the whole of it
    is two base64url segments and a signature, and because ES256 has one trap
    worth being explicit about: ``cryptography`` signs to a DER structure,
    while JWS wants the raw ``r || s`` pair, fixed-width. Emitting the DER
    bytes straight into the token produces something that looks like a JWT and
    that Apple rejects as malformed.
    """
    now = int(time.time()) if issued_at is None else issued_at
    header = {"alg": "ES256", "kid": settings.key_id}
    claims = {"iss": settings.team_id, "iat": now}

    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")

    key = _load_key(settings.private_key_pem)
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    # P-256: each half is exactly 32 bytes, left-padded. Trimming a leading
    # zero here is what makes an occasional token fail and the rest work.
    size = (key.curve.key_size + 7) // 8
    signature = r.to_bytes(size, "big") + s.to_bytes(size, "big")

    segments.append(_b64url(signature))
    return ".".join(segments)


class ProviderTokenCache:
    """One cached provider token per credential, renewed on a timer.

    Apple treats a provider token as a reusable bearer credential and will
    refuse to mint them faster than one per 20 minutes, so signing per push is
    not merely wasteful — it earns a ``TooManyProviderTokenUpdates``.  Keyed by
    credential so that changing the key in the admin UI takes effect on the
    next push rather than at the end of the current token's life.

    Held on :class:`~cplus_service.api.state.AppState` rather than in a module
    global, so tests get a fresh one per app and two processes never disagree
    about what is cached.
    """

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str, str], tuple[str, float]] = {}

    @staticmethod
    def _key(settings: ApnsSettings) -> tuple[str, str, str]:
        return (settings.team_id, settings.key_id, settings.private_key_pem)

    def get(self, settings: ApnsSettings, *, now: float | None = None) -> str:
        """The current token for ``settings``, minting one if it has aged out."""
        moment = time.time() if now is None else now
        cache_key = self._key(settings)

        cached = self._tokens.get(cache_key)
        if cached is not None:
            token, issued_at = cached
            if moment - issued_at < PROVIDER_TOKEN_LIFETIME_SECONDS:
                return token

        token = sign_provider_token(settings, issued_at=int(moment))
        self._tokens[cache_key] = (token, moment)
        return token

    def invalidate(self, settings: ApnsSettings) -> None:
        """Drop the cached token, so the next push signs a fresh one.

        Called when Apple says the token expired — which can happen despite the
        timer if the process was suspended, or the clock moved.
        """
        self._tokens.pop(self._key(settings), None)


def build_payload(notification: Notification) -> dict[str, Any]:
    """The APNs JSON body for one notification.

    ``title`` and ``subtitle`` and no ``body``: see this module's package
    docstring for why the third line is deliberately absent.
    """
    payload: dict[str, Any] = {
        "aps": {
            "alert": {
                "title": notification.title,
                "subtitle": notification.subtitle,
            },
            "sound": "default",
        }
    }
    if notification.data:
        payload["cplus"] = notification.data
    return payload


class ApnsClient:
    """Sends one notification to one device token.

    Deliberately not a fan-out: the caller owns the device list and what to do
    about each result, and a per-device outcome is the only thing APNs gives
    back anyway.
    """

    def __init__(
        self,
        settings: ApnsSettings,
        *,
        client: httpx.AsyncClient,
        tokens: ProviderTokenCache,
        production_host: str = PRODUCTION_HOST,
        sandbox_host: str = SANDBOX_HOST,
    ) -> None:
        self.settings = settings
        self._client = client
        self._tokens = tokens
        self._production_host = production_host
        self._sandbox_host = sandbox_host

    def _host(self, environment: ApnsEnvironment | str) -> str:
        if str(environment) == ApnsEnvironment.SANDBOX.value:
            return self._sandbox_host
        return self._production_host

    async def send(
        self,
        notification: Notification,
        *,
        device_token: str,
        environment: ApnsEnvironment | str = ApnsEnvironment.PRODUCTION,
        collapse_id: str | None = None,
    ) -> SendResult:
        """Push ``notification`` to one device, retrying once where it helps.

        One retry, not a loop: the two things worth retrying are a stale
        provider token (mint a new one, try again) and Apple throttling or
        faulting (try again). Anything still failing after that is either our
        bug or Apple being down, and hammering it helps neither.
        """
        payload = build_payload(notification)
        url = f"{self._host(environment)}/3/device/{device_token}"

        result = await self._attempt(url, payload, collapse_id=collapse_id)

        should_retry = result.status_code in RETRYABLE_STATUSES or (
            result.status_code == 403 and result.reason == EXPIRED_TOKEN_REASON
        )
        if result.outcome is SendOutcome.FAILED and should_retry:
            if result.reason == EXPIRED_TOKEN_REASON:
                self._tokens.invalidate(self.settings)
            result = await self._attempt(url, payload, collapse_id=collapse_id)

        return result

    async def _attempt(
        self, url: str, payload: dict[str, Any], *, collapse_id: str | None
    ) -> SendResult:
        headers = {
            "authorization": f"bearer {self._tokens.get(self.settings)}",
            "apns-topic": self.settings.bundle_id,
            "apns-push-type": "alert",
            # 10 = deliver now. These are user-visible alerts about something
            # that just happened; there is nothing to coalesce or defer.
            "apns-priority": "10",
            # 0 = do not store and retry. A notification about a request filed
            # ten minutes ago is not worth waking a phone for later.
            "apns-expiration": "0",
        }
        if collapse_id is not None:
            headers["apns-collapse-id"] = collapse_id

        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("APNs request failed: %s", exc)
            return SendResult(outcome=SendOutcome.FAILED, reason=str(exc))

        apns_id = response.headers.get("apns-id")
        if response.status_code == 200:
            return SendResult(
                outcome=SendOutcome.DELIVERED, status_code=200, apns_id=apns_id
            )

        reason = _reason_of(response)
        if response.status_code == 410 or reason in DEAD_TOKEN_REASONS:
            return SendResult(
                outcome=SendOutcome.UNREGISTERED,
                status_code=response.status_code,
                reason=reason,
                apns_id=apns_id,
            )

        logger.warning(
            "APNs rejected a push: status=%s reason=%s", response.status_code, reason
        )
        return SendResult(
            outcome=SendOutcome.FAILED,
            status_code=response.status_code,
            reason=reason,
            apns_id=apns_id,
        )


def _reason_of(response: httpx.Response) -> str | None:
    """Apple's ``reason`` string, if the error body was the JSON they document."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        reason = body.get("reason")
        if isinstance(reason, str):
            return reason
    return None


__all__ = [
    "ApnsClient",
    "ApnsConfigError",
    "ApnsSettings",
    "ProviderTokenCache",
    "SendOutcome",
    "SendResult",
    "build_payload",
    "sign_provider_token",
]
