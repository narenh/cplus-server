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
  access-control boundary over devices.  That is precisely why no admin is
  asked to handle one: :func:`enrol` obtains it automatically the moment
  notifications are switched on.  A credential that protects nothing the admin
  chose is friction, not security, and putting it in a settings form only made
  it look like it mattered;
* the relay sees notification text in plaintext.  APNs requires that — Apple
  has to read the alert to display it — so there is no arrangement in which the
  relay forwards without seeing.  That is why enabling this is an explicit,
  default-off decision on the Notifications tab: the consent that matters is
  about the plaintext, not about a key.

What the relay gives back that matters here is
:attr:`SendOutcome.UNREGISTERED`: the relay stores no device tokens, so only
this install can delete a dead one, and it can only do that if it is told.
:mod:`cplus_service.notify.service` is what acts on that.

Nothing in this module reads configuration or touches the database.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from ..db.models import ApnsEnvironment, Config
from .messages import Notification

logger = logging.getLogger(__name__)

#: The relay every install talks to.
#:
#: Deliberately **not** an admin-facing setting.  Running a relay means holding
#: an Apple Developer account's signing key, so in practice nobody self-hosting
#: this service will ever run their own — a text box for it was a field that
#: only ever held one value, sitting next to a credential nobody wanted, in a
#: form that made both look like decisions.
#:
#: The environment variable exists for development and for a fork with its own
#: Apple account, which are the only two cases that were ever real.  It is read
#: per call rather than cached so a test can point it somewhere else without
#: rebuilding the app.
DEFAULT_RELAY_URL = "https://apns.canopysf.com"

RELAY_URL_ENV = "CPLUS_RELAY_URL"

#: The relay answers 200 whenever Apple answered — including a rejection —
#: because forwarding a push that Apple refused is still a successful forward.
#: Anything else is the relay's own problem, not a verdict on the device token,
#: and must never be read as one.
_PUSH_PATH = "/v1/push"
_ENROL_PATH = "/v1/instances"


def relay_base_url() -> str:
    """Where to reach the relay, with no trailing slash."""
    return (os.environ.get(RELAY_URL_ENV) or DEFAULT_RELAY_URL).strip().rstrip("/")


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

        Two ways to get ``None``, and they are deliberately indistinguishable
        to callers: the master switch is off, or enrollment has not produced a
        key yet. Both mean the same thing to the sending path — there is
        nowhere to send — and the Notifications tab is where an admin finds out
        which one they are in.

        A missing key is now a *transient* state rather than an unfinished
        setup step: switching notifications on enrols, so the only way to be
        enabled without a key is for that call to have failed.
        """
        if not config.notifications_enabled:
            return None

        api_key = (config.notification_relay_api_key or "").strip()
        if not api_key:
            return None

        return cls(url=relay_base_url(), api_key=api_key)

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
class Enrollment:
    """A relay identity this install now holds.

    ``ready`` is the relay reporting on *itself*: the key is good either way,
    but until the relay operator has installed a signing key there is nothing
    behind it. Worth surfacing, because an admin who has done everything right
    and still sees no notifications deserves to be told it is not their end.
    """

    instance_id: str
    api_key: str
    bundle_id: str | None = None
    ready: bool = True


class EnrollmentError(Exception):
    """Enrolling with the relay did not work.

    Carries a sentence written for an admin looking at the Notifications tab,
    since that is the only place it is ever shown.
    """ 


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


async def enrol(*, client: httpx.AsyncClient) -> Enrollment:
    """Obtain a fresh relay identity for this install.

    Called when an admin switches notifications on. This is the whole of the
    setup that used to be a URL, an API key, a Save button and a Check button:
    a single unauthenticated POST that hands back an identity.

    Raises :class:`EnrollmentError` with something an admin can act on. The
    caller is expected to leave notifications *off* when this fails — an
    install that is switched on with no key would sit there silently sending
    nothing, which is the exact failure the old settings form was so good at
    producing.
    """
    url = f"{relay_base_url()}{_ENROL_PATH}"

    try:
        response = await client.post(url)
    except httpx.HTTPError as exc:
        raise EnrollmentError(
            f"Could not reach the notification relay at {relay_base_url()}. "
            f"Check this server's outbound connectivity and try again. ({exc})"
        ) from exc

    if response.status_code == 403:
        raise EnrollmentError(
            "The notification relay is not issuing new keys at the moment. "
            "This is not something you can fix from here — try again later."
        )

    if response.status_code == 429:
        raise EnrollmentError(
            "The notification relay is rate-limiting requests from this "
            "address. Wait a minute and try again."
        )

    if response.status_code not in (200, 201):
        detail = _detail_of(response) or f"it answered {response.status_code}"
        raise EnrollmentError(f"The notification relay refused to enrol us: {detail}")

    body = _json_of(response)
    instance_id = _str_or_none(body.get("instance_id"))
    api_key = _str_or_none(body.get("api_key"))

    if not instance_id or not api_key:
        # A 2xx we cannot use is worse than an error, because everything
        # downstream would behave as though setup had succeeded.
        raise EnrollmentError(
            "The notification relay returned a response we did not understand. "
            "It may be running a newer version than this server expects."
        )

    return Enrollment(
        instance_id=instance_id,
        api_key=api_key,
        bundle_id=_str_or_none(body.get("bundle_id")),
        ready=bool(body.get("ready", True)),
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
    "RELAY_URL_ENV",
    "Enrollment",
    "EnrollmentError",
    "RelayClient",
    "RelaySettings",
    "SendOutcome",
    "SendResult",
    "build_request",
    "enrol",
    "relay_base_url",
]
