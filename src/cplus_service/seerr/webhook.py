"""Reading the webhook Seerr posts when something happens to a request.

The mirror image of :mod:`cplus_service.api.routes.request`. That path is a
request arriving *through* this service on its way to Seerr, where we know who
the caller is because they authenticated with us. This one is a request that
never came near us — someone opened Seerr and pressed the button there — and
Seerr telling us about it afterwards.

Everything here is parsing, and it is deliberately forgiving, because the shape
of the payload is not ours. Seerr's webhook body is a **user-editable JSON
template**: the admin can add fields, remove fields, or paste something from a
blog post. The stock template is what this is written against, and anything
missing from it reads as unknown rather than as an error — a notification that
says slightly less is worth more than a 400 that makes Seerr retry and then give
up.

Two details of the stock template are worth knowing:

* Every value arrives as a **string**, including ids, because the template is
  substituted into JSON text. ``"request_id": "42"`` is the normal case, not a
  malformed one.
* The ``{{media}}``/``{{request}}``/``{{issue}}`` keys are markers Seerr
  rewrites on the way out: the key becomes ``media``/``request``/``issue`` when
  that object applies to the event and the whole entry is dropped when it does
  not. So a test notification genuinely has no ``request`` object, rather than
  an empty one.

A variable Seerr does not recognise is left in the body verbatim, braces and
all, so ``{{requestedBy_username}}`` is a value this has to reject rather than
treat as somebody's name.

**There is no user id in any of it.** Seerr's template vocabulary offers a
username, an email and an avatar, and this service cannot ask Seerr who that is
— it holds no Seerr credential of its own (see :mod:`cplus_service.api.routes.seerr`).
Matching a payload to a local user is therefore by name, done by the caller, and
allowed to fail: an unmatched request is still logged and still notified, just
attributed to whatever Seerr called them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The notification types that mean "a request has just been filed".
#:
#: ``MEDIA_PENDING`` is a request waiting for approval; ``MEDIA_AUTO_APPROVED``
#: is one Seerr approved on the spot because of the requester's permissions.
#: Both are somebody asking for something, which is the event an admin wants to
#: hear about — the difference is only whether there is anything left to do.
REQUEST_FILED_TYPES = frozenset({"MEDIA_PENDING", "MEDIA_AUTO_APPROVED"})

#: What Seerr's own "Test" button sends. Acknowledged, never acted on.
TEST_TYPE = "TEST_NOTIFICATION"

#: ``Title (2026)`` — the shape of the ``subject`` line for a media event.
_TRAILING_YEAR = re.compile(r"\s*\(((?:19|20)\d{2})\)$")

#: An unsubstituted template variable, which is a value to ignore rather than
#: to believe: Seerr leaves these verbatim when it does not know the name.
_UNRESOLVED = re.compile(r"^\{\{.*\}\}$")


@dataclass(frozen=True)
class SeerrWebhookEvent:
    """One delivery, reduced to the facts this service acts on.

    Every field but :attr:`notification_type` is optional, and each is optional
    for a real reason rather than out of caution: a custom payload template can
    omit any of them, and a test notification carries none of them.
    """

    notification_type: str
    subject: str | None = None
    request_id: int | None = None
    username: str | None = None
    email: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None

    @property
    def is_request_filed(self) -> bool:
        """Whether this is somebody filing a request."""
        return self.notification_type in REQUEST_FILED_TYPES

    @property
    def is_test(self) -> bool:
        """Whether this is Seerr's own Test button."""
        return self.notification_type == TEST_TYPE


def parse_event(payload: Mapping[str, Any]) -> SeerrWebhookEvent:
    """Read a delivered webhook body.

    Looks inside the ``request`` and ``media`` objects the stock template
    produces, and falls back to the same names at the top level, which is where
    they land if an admin flattened the template. Never raises: an
    unrecognisable body becomes an event with an empty type, which no branch in
    the handler acts on.
    """
    request = _object(payload, "request")
    media = _object(payload, "media")

    return SeerrWebhookEvent(
        notification_type=_text(payload.get("notification_type")) or "",
        subject=_text(payload.get("subject")),
        request_id=_int(_first(request.get("request_id"), payload.get("request_id"))),
        username=_text(
            _first(
                request.get("requestedBy_username"),
                payload.get("requestedBy_username"),
            )
        ),
        email=_text(
            _first(request.get("requestedBy_email"), payload.get("requestedBy_email"))
        ),
        tmdb_id=_int(_first(media.get("tmdbId"), payload.get("media_tmdbid"))),
        media_type=_text(_first(media.get("media_type"), payload.get("media_type"))),
    )


def split_year(subject: str) -> tuple[str, int | None]:
    """Split ``Title (2026)`` into its two halves.

    Seerr builds the subject line itself and always writes it this way for a
    media event, so this recovers the structure rather than guessing at it. A
    subject with no year — or one whose year is not in the parentheses at the
    end — comes back whole, which renders identically.
    """
    match = _TRAILING_YEAR.search(subject)
    if not match:
        return subject.strip(), None

    title = subject[: match.start()].strip()
    if not title:
        return subject.strip(), None
    return title, int(match.group(1))


def _object(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """One of the template's nested objects, or an empty stand-in for it."""
    value = payload.get(name)
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any) -> Any:
    """The first value that is present at all. ``None`` if none is."""
    for value in values:
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    """A non-empty string, or ``None`` for anything that is not one."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or _UNRESOLVED.match(stripped):
        return None
    return stripped


def _int(value: Any) -> int | None:
    """An id, whether it arrived as a number or as the string of one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if text is None or not text.isdigit():
        return None
    return int(text)


__all__ = [
    "REQUEST_FILED_TYPES",
    "TEST_TYPE",
    "SeerrWebhookEvent",
    "parse_event",
    "split_year",
]
