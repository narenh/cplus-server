"""Turning an event into the two lines an iOS notification shows.

Every notification this service sends has the same shape::

    The End of Oak Street (2026)      <- aps.alert.title
    Requested by Jane Dietrich        <- aps.alert.subtitle

The first line identifies *what*, the second *who and how*.  Only the second
line varies by type, which is why the switch list in the admin UI can describe
a type by its subtitle alone.

The alert carries no ``body``.  iOS is happy to render a title/subtitle pair
with nothing under it, and there is no third fact worth a third line — padding
it out with the release name would bury the part that matters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..release.parser import parse_title
from .types import NotificationType

#: Words a title-caser should leave lowercase unless they lead the title.  Only
#: reached by the release-title fallback below, never by a client-supplied name.
_MINOR_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
        "nor", "of", "on", "onto", "or", "over", "the", "to", "up", "vs", "with",
    }
)

_TRAILING_YEAR = re.compile(r"\s*(?<![0-9])((?:19|20)\d{2})$")


@dataclass(frozen=True)
class MediaSummary:
    """What a notification's first line is built from.

    ``year`` is optional because it genuinely can be unknown — an indexer's
    release name may omit it and a client need not send one.  A missing year
    drops the parenthetical rather than printing an empty one.
    """

    title: str
    year: int | None = None

    @property
    def display(self) -> str:
        """``Title (Year)``, or just ``Title`` when the year is unknown."""
        if self.year is None:
            return self.title
        return f"{self.title} ({self.year})"


@dataclass(frozen=True)
class Notification:
    """A rendered notification, independent of how it is delivered.

    APNs is the only transport today, but this carries no APNs vocabulary: it
    is the two display lines plus the structured facts a client may want to
    route on.  :mod:`cplus_service.notify.apns` is what turns it into a
    payload.
    """

    type: NotificationType
    title: str
    subtitle: str
    data: dict[str, Any] = field(default_factory=dict)
    """Extra keys sent alongside ``aps``, so the app can deep-link to the thing
    the notification is about instead of re-deriving it from the text."""


def title_case(text: str) -> str:
    """Best-effort display casing for a name recovered from a release title.

    Deliberately simple: capitalise every word except short joining words in a
    non-leading position.  It will get ``iPhone`` and ``REC`` wrong, which is
    acceptable for a fallback that only runs when the client did not tell us
    the real name.
    """
    words = text.split()
    cased: list[str] = []
    for index, word in enumerate(words):
        if index > 0 and word in _MINOR_WORDS:
            cased.append(word)
        else:
            cased.append(word[:1].upper() + word[1:])
    return " ".join(cased)


def media_from_release_title(release_title: str) -> MediaSummary:
    """Recover a display name and year from a scene release name.

    The fallback for a grab whose client did not send the real title.  The
    parser already knows where a movie name ends — its ``base_title`` is the
    normalised name up to and including the year — so this only has to split
    the year back off and re-case the rest.

    Best-effort by nature.  When even the parser finds nothing usable, the raw
    release title is returned unchanged: a notification reading like an indexer
    listing is still better than one reading ``Unknown``.
    """
    base = parse_title(release_title).base_title
    if not base:
        return MediaSummary(title=release_title.strip() or release_title)

    year: int | None = None
    match = _TRAILING_YEAR.search(base)
    if match:
        year = int(match.group(1))
        base = base[: match.start()]

    name = title_case(base.strip())
    if not name:
        return MediaSummary(title=release_title.strip() or release_title, year=year)
    return MediaSummary(title=name, year=year)


def user_requested(media: MediaSummary, *, username: str, **data: Any) -> Notification:
    """"Jane Dietrich filed a request" — the built-in Request action."""
    return Notification(
        type=NotificationType.USER_REQUESTED,
        title=media.display,
        subtitle=f"Requested by {username}",
        data={"type": NotificationType.USER_REQUESTED.value, **data},
    )


def user_action(
    media: MediaSummary, *, username: str, action_name: str, **data: Any
) -> Notification:
    """"Jane Dietrich ran Stream Now" — any Prowlarr-backed action."""
    return Notification(
        type=NotificationType.USER_ACTION,
        title=media.display,
        subtitle=f"{username}: {action_name}",
        data={"type": NotificationType.USER_ACTION.value, **data},
    )


__all__ = [
    "MediaSummary",
    "Notification",
    "media_from_release_title",
    "title_case",
    "user_action",
    "user_requested",
]
