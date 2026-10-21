"""The playback event Canopy+ posts, and the body this service forwards.

**The inbound shape is a contract with Canopy+**, built against it in parallel
with this service, so field names here do not change without the app changing
with them. It is deliberately close to what is forwarded: an automation author
reading the Playback tab's example sees the app's own words, plus the handful
of fields only the server can add (who, when, and from where).

Two rules about leniency, pulling in opposite directions:

* **An unknown event is refused (422).** The event name is the one field every
  automation dispatches on, so forwarding a value nobody has documented would
  produce webhooks that silently match nothing. Refusing it makes a mismatch
  between app and server loud, in the app's own log, on the first try.
* **Everything else is forgiving.** Explicit ``null`` is accepted wherever a
  value is optional (Canopy+ sends ``"show_title": null`` for a movie rather
  than omitting it), unknown extra fields are ignored rather than refused so a
  newer app can talk to an older server, and a chapter's title may be missing,
  because Plex chapters frequently have none.

The one cross-field rule: a ``chapter`` event must say which chapter, since a
chapter change that does not name the chapter is nothing an automation can use.
The opposite cases — a ``chapter`` object on a pause, ``watched`` on anything
but ``stopped`` — are cleared to ``null`` rather than refused, so the forwarded
body only ever carries them where the documentation says they appear.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: What every forwarded body says in ``source``. Lets an automation that also
#: receives Plex's own webhooks, or a second app's, tell them apart.
SOURCE = "canopy-plus"

#: Bounds on client-supplied strings. Generous — the point is to keep one
#: misbehaving client from filling the in-memory log with megabytes, not to
#: decide how long a film's title may be.
MAX_TITLE_LENGTH = 512
MAX_ROOM_LENGTH = 128
MAX_IDENTIFIER_LENGTH = 256


class PlaybackEventKind(StrEnum):
    """Every event the player reports. Anything else is a 422.

    The first four are the ones Plex's own webhooks also have, under different
    names. The last four are the reason this feature exists.
    """

    STARTED = "started"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"
    CHAPTER = "chapter"
    CREDITS_STARTED = "credits_started"
    INTRO_STARTED = "intro_started"
    INTRO_ENDED = "intro_ended"


class PlaybackChapter(BaseModel):
    """The chapter just entered. Only on a ``chapter`` event.

    ``index`` counts from 0, as Plex's own chapter list does; ``start`` is in
    seconds from the start of the title, the same clock as ``position``.
    """

    index: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    start: float | None = Field(default=None, ge=0)


class PlaybackItem(BaseModel):
    """What is playing.

    ``rating_key`` is Plex's own id for the item, and the one field here an
    automation could use to look anything further up. Numbers are accepted for
    it and turned into strings, since a JSON encoder somewhere between the app
    and here may well have decided ``"10488"`` looked like a number.

    The episode fields are ``null`` for a movie, and ``year`` may be ``null``
    for either.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    rating_key: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    type: Literal["movie", "episode"]
    title: str = Field(max_length=MAX_TITLE_LENGTH)
    show_title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=0)
    year: int | None = Field(default=None, ge=0)


class PlaybackEvent(BaseModel):
    """``POST /playback/events`` — one moment in one Apple TV's playback.

    ``room`` is whatever the user typed into Canopy+'s settings ("Living
    Room"), or ``null`` if they never did. ``client_id`` is the Apple TV's Plex
    client identifier — stable per device, so it is what an automation should
    key on if rooms are unset or two devices share a name.

    ``position`` and ``duration`` are seconds. ``watched`` only means something
    on ``stopped``: true when the title was played to the end or left past 90%,
    which is Canopy+'s rule rather than this service's.
    """

    event: PlaybackEventKind
    room: str | None = Field(default=None, max_length=MAX_ROOM_LENGTH)
    client_id: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    position: float = Field(ge=0)
    duration: float = Field(ge=0)
    watched: bool | None = None
    chapter: PlaybackChapter | None = None
    item: PlaybackItem

    @model_validator(mode="after")
    def _fields_only_where_they_apply(self) -> PlaybackEvent:
        if self.event == PlaybackEventKind.CHAPTER:
            if self.chapter is None:
                raise ValueError("a chapter event must include the chapter")
        else:
            self.chapter = None
        if self.event != PlaybackEventKind.STOPPED:
            self.watched = None
        # A blank room is the same as no room: an automation testing
        # ``room == null`` should not have to test for "" as well.
        if self.room is not None and not self.room.strip():
            self.room = None
        elif self.room is not None:
            self.room = self.room.strip()
        return self


def outbound_payload(
    event: PlaybackEvent,
    *,
    user_name: str,
    profile: str,
    received_at: datetime,
    test: bool = False,
) -> dict[str, Any]:
    """The JSON body forwarded to every webhook.

    The event exactly as validated — every key present, ``null`` where it does
    not apply — plus what only the server knows:

    * ``user`` — the cplus user whose Plex token sent it, and the Plex Home
      profile header if the app sent one (``""`` otherwise; Canopy+ does not
      send one today).
    * ``received_at`` — when this service received it, ISO 8601 in UTC with a
      ``Z``. The app's own clock is not trusted for this, and the event carries
      no timestamp of its own.
    * ``source`` — always ``"canopy-plus"``.
    * ``test`` — true only for the Playback tab's "Send test event" button, so
      an automation can choose to ignore it. Always present, so a condition on
      it never has to allow for the key being missing.
    """
    stamp = received_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        **event.model_dump(mode="json"),
        "user": {"name": user_name, "profile": profile},
        "received_at": stamp,
        "source": SOURCE,
        "test": test,
    }


def sample_event() -> PlaybackEvent:
    """The event the "Send test event" button forwards, and the page's example.

    A ``chapter`` event, because it is the most complete shape — the one that
    carries the ``chapter`` object — and so the most useful single example to
    write an automation against. The same object feeds the example on the page
    and the button, so the two cannot show different things.
    """
    return PlaybackEvent(
        event=PlaybackEventKind.CHAPTER,
        room="Living Room",
        client_id="canopy-plus-test-event",
        position=1234.5,
        duration=8040.2,
        chapter=PlaybackChapter(index=3, title="The Battle", start=1200.0),
        item=PlaybackItem(
            rating_key="10488",
            type="movie",
            title="The End of Oak Street",
            year=2026,
        ),
    )


def describe_item(item: dict[str, Any]) -> str:
    """One line naming what was playing, for the Playback tab's recent list.

    Takes the forwarded dict rather than the model, because that is what the
    in-memory log keeps: ``Show · S1E2 · Episode`` for an episode, ``Title
    (Year)`` for a movie.
    """
    title = item.get("title") or "Untitled"
    if item.get("type") == "episode":
        parts = [item.get("show_title")]
        season, episode = item.get("season"), item.get("episode")
        if season is not None and episode is not None:
            parts.append(f"S{season}E{episode}")
        parts.append(title)
        return " · ".join(part for part in parts if part)
    year = item.get("year")
    return f"{title} ({year})" if year else title
