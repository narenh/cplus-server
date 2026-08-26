"""Sending a notification through the public forwarding relay.

This install has no APNs signing key and cannot have one.  The key belongs to
the Apple Developer account that owns the app and signs pushes for that whole
team, so it stays on one machine its owner runs — a relay
(:data:`DEFAULT_RELAY_URL` by default) — and every self-hosted install hands
that relay a device token and two lines of text over an authenticated call.

**Isolation between installs comes from token custody, not from the relay.**
An APNs device token is per-device, per-app and unguessable, and this install
only ever learns the tokens its own logged-in users hand it.  Another install
cannot notify this one's users because it has never seen their tokens — not
because the relay is enforcing a routing rule.  The relay keeps no
device-to-install mapping at all.  Two things follow that are worth having in
mind while reading this module:

* the relay API key is a rate-limit identity and an abuse handle, **not** an
  access-control boundary over devices;
* the relay sees notification text in plaintext.  APNs requires that — Apple
  has to read the alert to display it — so there is no arrangement in which the
  relay forwards without seeing.  That is why enabling this is an explicit,
  default-off decision on the Notifications tab rather than something that
  happens once a key is pasted in.

What the relay gives back that matters here is
:attr:`SendOutcome.UNREGISTERED`: the relay stores no device tokens, so only
this install can delete a dead one, and it can only do that if it is told.
:mod:`cplus_service.notify.service` is what acts on that.

Nothing in this module reads configuration or touches the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from ..db.models import ApnsEnvironment, Config
from .messages import Notification

logger = logging.getLogger(__name__)

#: Where an install points unless its admin says otherwise.  Not baked into the
#: code paths — it is a default for the settings field, so an operator running
#: their own relay, or a fork with its own Apple Developer account, only has to
#: change one text box.
DEFAULT_RELAY_URL = "https://apns.canopysf.com"

#: The relay answers 200 whenever Apple answered — including a rejection —
#: because forwarding a push that Apple refused is still a successful forward.
#: Anything else is the relay's own problem, not a verdict on the device token,
#: and must never be read as one.
_PUSH_PATH = "/v1/push"
_VERIFY_PATH = "/v1/verify"


@dataclass(frozen=True)
class RelaySettings:
    """Everything needed to reach the relay.

    Built from the config row via :meth:`from_config`, which returns ``None``
    when notifications are off or unconfigured.  That is the default state of
    every install, so it is a ``None``, not an exception.
    """

    url: str
    api_key: str

    @classmethod
    def from_config(cls, config: Config) -> RelaySettings | None:
        """The relay to send through, or ``None`` if this install is not set up.

        Three ways to get ``None``, and they are deliberately indistinguishable
        to callers: the master switch is off, no API key has been entered, or
        the URL has been blanked. All three mean the same thing to the sending
        path — there is nowhere to send — and the Notifications tab is where an
        admin finds out which one they are in.
        """
        if not config.notifications_enabled:
            return None

        api_key = (config.notification_relay_api_key or "").strip()
        url = (config.notification_relay_url or DEFAULT_RELAY_URL).strip()
        if not api_key or not url:
            return None

        return cls(url=url.rstrip("/"), api_key=api_key)

    def endpoint(self, path: str) -> str:
        return f"{self.url}{path}"


class SendOutcome(StrEnum):
    """What became of one push."""

    DELIVERED = "delivered"

    UNREGISTERED = "unregistered"
    """Apple says this device token is dead. Delete it.

    The relay cannot: it never stored the token. If this install ignores the
    outcome, nothing else will clean up after it."""

    FAILED = "failed"


@dataclass(frozen=True)
class SendResult:
    """The outcome of one push, with enough detail to log it usefully."""

    outcome: SendOutcome
    status_code: int | None = None
    """The *relay's* HTTP status, not Apple's. 200 means the relay forwarded;
    read :attr:`outcome` for what Apple then said."""

    reason: str | None = None
    """Why it failed, in whatever detail is available — Apple's machine-readable
    reason by way of the relay, or the relay's own complaint."""

    apns_id: str | None = None
    """Apple's id for the push, for correlating with their delivery console."""

    @property
    def delivered(self) -> bool:
        return self.outcome is SendOutcome.DELIVERED


@dataclass(frozen=True)
class VerifyResult:
    """What ``GET /v1/verify`` said, flattened into something a page can show."""

    ok: bool
    message: str
    instance: str | None = None
    bundle_id: str | None = None


def build_request(
    notification: Notification,
    *,
    device_token: str,
    environment: ApnsEnvironment | str = ApnsEnvironment.PRODUCTION,
    collapse_id: str | None = None,
) -> dict[str, Any]:
    """The relay's request body for one notification.

    Text and an opaque blob — deliberately not an APNs payload. The relay
    builds the ``aps`` dictionary itself and refuses one sent from here, so
    that no install can send a silent background wake signed with the relay
    operator's key. Nothing is lost by that: everything this service sends is
    a user-visible alert.
    """
    body: dict[str, Any] = {
        "device_token": device_token,
        "environment": str(environment),
        "title": notification.title,
        "subtitle": notification.subtitle,
    }
    if notification.data:
        body["data"] = notification.data
    if collapse_id is not None:
        body["collapse_id"] = collapse_id
    return body


class RelayClient:
    """Sends one notification to one device, through the relay.

    Deliberately not a fan-out: the caller owns the device list and what to do
    about each result, and a per-device outcome is the only thing the relay
    gives back anyway.
    """

    def __init__(self, settings: RelaySettings, *, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.settings.api_key}"}

    async def send(
        self,
        notification: Notification,
        *,
        device_token: str,
        environment: ApnsEnvironment | str = ApnsEnvironment.PRODUCTION,
        collapse_id: str | None = None,
    ) -> SendResult:
        """Push ``notification`` to one device.

        No retry. The relay already retries what is worth retrying against
        Apple — a stale provider token, a throttle — and it knows things this
        side cannot, like whether the token was even minted. Retrying again
        from here would double a burst the relay is rate-limiting us for, on
        a background task nobody is waiting on.
        """
        body = build_request(
            notification,
            device_token=device_token,
            environment=environment,
            collapse_id=collapse_id,
        )

        try:
            response = await self._client.post(
                self.settings.endpoint(_PUSH_PATH), json=body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            logger.warning("could not reach the notification relay: %s", exc)
            return SendResult(outcome=SendOutcome.FAILED, reason=str(exc))

        if response.status_code != 200:
            reason = _detail_of(response) or f"the relay answered {response.status_code}"
            logger.warning("the notification relay refused a push: %s", reason)
            return SendResult(
                outcome=SendOutcome.FAILED,
                status_code=response.status_code,
                reason=reason,
            )

        return _result_of(response)


async def verify(
    settings: RelaySettings, *, client: httpx.AsyncClient
) -> VerifyResult:
    """Ask the relay whether this install's key works, for the settings page.

    Reports the three states separately because they have three different
    owners: the key is wrong (this admin fixes it), the relay is up but has no
    signing key of its own (the relay operator fixes it), or the relay cannot
    be reached at all (nobody knows yet). Collapsing them into "it didn't work"
    is how an admin spends an afternoon re-pasting a key that was already fine.
    """
    try:
        response = await client.get(
            settings.endpoint(_VERIFY_PATH),
            headers={"authorization": f"Bearer {settings.api_key}"},
        )
    except httpx.HTTPError as exc:
        return VerifyResult(
            ok=False, message=f"Could not reach the relay at {settings.url}: {exc}"
        )

    if response.status_code == 401:
        return VerifyResult(
            ok=False,
            message=(
                "The relay does not recognise this API key. Check it was pasted "
                "in full, and ask whoever issued it whether it is still valid."
            ),
        )

    if response.status_code != 200:
        detail = _detail_of(response) or f"it answered {response.status_code}"
        return VerifyResult(ok=False, message=f"The relay refused the check: {detail}")

    body = _json_of(response)
    instance = _str_or_none(body.get("instance"))
    bundle_id = _str_or_none(body.get("bundle_id"))

    if not body.get("ready", True):
        return VerifyResult(
            ok=False,
            instance=instance,
            message=(
                "This key works, but the relay has no APNs signing key of its "
                "own yet, so nothing can be delivered. Nothing is wrong on this "
                "end — the relay's operator has to finish setting it up."
            ),
        )

    suffix = f" as “{instance}”" if instance else ""
    topic = f" Pushes go to {bundle_id}." if bundle_id else ""
    return VerifyResult(
        ok=True,
        instance=instance,
        bundle_id=bundle_id,
        message=f"The relay accepted this key{suffix}.{topic}",
    )


def _json_of(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _detail_of(response: httpx.Response) -> str | None:
    """FastAPI's ``detail`` from an error body, when there was one."""
    return _str_or_none(_json_of(response).get("detail"))


def _result_of(response: httpx.Response) -> SendResult:
    """Read a 200 from the relay into a :class:`SendResult`.

    An unrecognised ``result`` is treated as a failure rather than a delivery.
    Guessing "probably fine" from a relay speaking a dialect we do not know is
    how a device token that Apple has rejected stays in the table forever.
    """
    body = _json_of(response)
    reason = _str_or_none(body.get("reason"))
    apns_id = _str_or_none(body.get("apns_id"))

    raw = body.get("result")
    try:
        outcome = SendOutcome(raw)
    except ValueError:
        logger.warning("the notification relay reported an unknown result: %r", raw)
        return SendResult(
            outcome=SendOutcome.FAILED,
            status_code=200,
            reason=f"the relay reported an unrecognised result: {raw!r}",
        )

    return SendResult(
        outcome=outcome, status_code=200, reason=reason, apns_id=apns_id
    )


__all__ = [
    "DEFAULT_RELAY_URL",
    "RelayClient",
    "RelaySettings",
    "SendOutcome",
    "SendResult",
    "VerifyResult",
    "build_request",
    "verify",
]
