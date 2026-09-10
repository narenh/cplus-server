"""The Home document — one shape, shared by everything that reads or writes it.

CanopyPlus stores its whole home screen as a single versioned document — its
own ``HomeSettings``: the carousel and its two switches, the shelf list, the
Top Shelf, and one ``modifiedAt`` covering all of it — and merges it
whole-document last-write-wins. This module is that same document on this side
of the wire. :func:`document` projects either one user's own
``UserHomeSettings`` row or the admin's global ``Config`` into exactly the JSON
CanopyPlus's own decoder expects; :func:`apply` writes one back.

Three callers share it, which is why it lives here rather than inside any one
of them:

* ``GET``/``PUT /home`` — the per-user sync (:mod:`.api.routes.home`);
* ``GET /register?first_run=true`` — the fresh-install seed
  (:mod:`.api.routes.defaults`);
* the admin web UI's own Home editors, which mutate the very same rows and
  stamp them through :func:`touched` (see :mod:`.api.routes.admin.shelf_rows`
  and :mod:`.api.routes.admin.libraries`).

**Every shelf carries all seven keys, nulls included.** CanopyPlus's
``HomeShelfDataModel.init(from:)`` is a *strict* decoder — it calls
``decode(String?.self, forKey: .discoverHubKey)``, which throws ``keyNotFound``
on a missing key rather than yielding ``nil``. So a shelf with no Discover hub
must serialise as ``"discoverHubKey": null`` and never be omitted. (Its parent
``HomeSettings`` decodes leniently and *does* tolerate a missing key; the
shelves inside it do not. Only ``modifiedAt`` may be left out — see below.)

**Timestamps are whole seconds, in UTC, always.** Two separate reasons, and
both of them bite:

* CanopyPlus decodes with ``JSONDecoder.dateDecodingStrategy = .iso8601``,
  which is ``ISO8601DateFormatter``'s ``.withInternetDateTime`` — and that
  **rejects fractional seconds**. A stamp carrying microseconds fails the
  client's decode outright.
* If a stored stamp kept a microsecond component that the wire format then
  dropped, every document the client echoed back would look *older* than what
  is on file and would lose its own merge, permanently. So :func:`touched`
  truncates when it *writes*, not when it serialises, and the two can never
  disagree.

**Stored timestamps come back naive.** SQLite has no timestamp type, so
``DateTime(timezone=True)`` round-trips as a plain string and SQLAlchemy hands
back a ``datetime`` with ``tzinfo=None`` — while a stamp parsed off the wire is
offset-aware. Comparing the two raises ``TypeError``, which on a merge path
would be a 500 on the first ``PUT`` anyone made. Everything that writes a stamp
here writes UTC, so :func:`as_utc` reattaches it, and nothing compares two
stamps without going through it first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from .bootstrap import upnext_shelf
from .db.models import Config, UserHomeSettings
from .db.session import get_config

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, not behaviour
    from .api.schemas import HomeDocument

#: How a stamp is written on the wire. Second precision and a literal ``Z``,
#: because that is the whole of what ``ISO8601DateFormatter`` accepts by
#: default — see the module docstring.
WIRE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class HomeSettingsLike(Protocol):
    """Structural shape shared by ``Config`` and ``UserHomeSettings``.

    For type-checking only — both real classes already carry every one of
    these fields with no shared base class, since ``Config`` carries a great
    deal else that has nothing to do with Home. Together these six columns are
    exactly what CanopyPlus's own ``HomeSettings`` document holds.
    """

    home_shelves: list[dict[str, Any]]
    home_carousel: dict[str, Any] | None
    home_carousel_enabled: bool
    home_carousel_include_on_deck: bool
    home_top_shelf: dict[str, Any] | None
    home_modified_at: datetime | None


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


def as_utc(value: datetime | None) -> datetime | None:
    """``value`` as an offset-aware UTC stamp, or ``None``.

    A naive input is *assumed* to already be UTC rather than converted from
    local time: the only naive stamps in play are ones SQLite handed back, and
    everything that writes one writes UTC. See the module docstring.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def now_stamp() -> datetime:
    """The current time, at the precision the wire format can carry."""
    return datetime.now(UTC).replace(microsecond=0)


def format_stamp(value: datetime) -> str:
    """``value`` in the one format CanopyPlus's decoder accepts."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(WIRE_TIMESTAMP_FORMAT)


def touched(home: HomeSettingsLike) -> None:
    """Stamp ``home`` as just edited, for the whole document at once.

    Every mutation of a Home — a shelf added, removed, reordered or edited, the
    Carousel's source or either of its switches, Top Shelf's source, or a whole
    document arriving at ``PUT /home`` — calls this exactly once after actually
    changing something, whether the target is the admin's global ``Config`` or
    one user's own ``UserHomeSettings``. Mirrors CanopyPlus's own
    ``HomeSettings.modifiedAt``: one timestamp for the five content fields
    together, not one per shelf — see ``db.models.Config.home_modified_at`` for
    why.

    Truncated to whole seconds by :func:`now_stamp`, which is a correctness
    requirement and not a cosmetic one; the module docstring says why.
    """
    home.home_modified_at = now_stamp()


# --------------------------------------------------------------------------- #
# Whose Home
# --------------------------------------------------------------------------- #


def seeded(user_id: int, config: Config) -> UserHomeSettings:
    """A fresh copy of the admin's current global Home, for one user.

    Not added to any session — callers decide that. ``home_modified_at`` is
    deliberately left unset: seeding is not an edit, the same way CanopyPlus's
    own ``HomeSettings()`` starts at ``.distantPast`` until a person actually
    changes something. The same "fall back to a fresh Continue Watching shelf"
    rule :func:`document` applies to any gap covers a global config that has
    never had a Carousel, Top Shelf or shelf list configured, so the seed is
    never itself empty.
    """
    return UserHomeSettings(
        user_id=user_id,
        home_shelves=[dict(shelf) for shelf in config.home_shelves] or [upnext_shelf()],
        home_carousel=dict(config.home_carousel) if config.home_carousel else upnext_shelf(),
        home_carousel_enabled=config.home_carousel_enabled,
        home_carousel_include_on_deck=config.home_carousel_include_on_deck,
        home_top_shelf=dict(config.home_top_shelf) if config.home_top_shelf else upnext_shelf(),
    )


async def get_own_home(session: AsyncSession, user_id: int) -> UserHomeSettings | None:
    """This user's own Home row, or ``None`` if they have never had one.

    A read that deliberately does **not** create. Absence is meaningful: a user
    with no row of their own still tracks the admin's global default, and
    forking them off it merely because something *read* their Home would
    silently freeze them at whatever the default happened to be that day. Only
    an actual edit forks — see :func:`get_or_create_home`.
    """
    return await session.get(UserHomeSettings, user_id)


async def effective_home(session: AsyncSession, user_id: int) -> HomeSettingsLike:
    """The Home document that applies to this user right now.

    Their own row if they have one, the admin's global default if they do not.
    The single answer to "whose Home is this?", shared by ``GET``/``PUT /home``
    and by ``GET /register``'s first-run bundle so the two can never disagree
    about what a user's Home currently is.

    Deliberately does not create the row — see :func:`get_own_home`.
    """
    own = await get_own_home(session, user_id)
    if own is not None:
        return own
    return await get_config(session)


async def get_or_create_home(
    session: AsyncSession, user_id: int, config: Config
) -> UserHomeSettings:
    """This user's own Home row, seeded from the global default if absent.

    Mirrors ``db.session.get_config``'s own get-or-create shape. Seeding happens
    exactly once, at creation, and this deliberately never re-seeds from
    ``Config`` afterwards: after the fork, editing a user's shelf never touches
    the global default and a later change to the global default never touches
    this user.
    """
    home = await get_own_home(session, user_id)
    if home is not None:
        return home

    home = seeded(user_id, config)
    session.add(home)
    await session.flush()
    return home


# --------------------------------------------------------------------------- #
# The wire shape
# --------------------------------------------------------------------------- #


def document(home: HomeSettingsLike) -> dict[str, Any]:
    """``home`` as one CanopyPlus ``HomeSettings`` document.

    Keys are the app's own Codable names, so what comes back decodes straight
    into ``HomeSettings`` with no translation on the client at all. Built by
    hand rather than dumped from a Pydantic model because the two ends of this
    shape have different rules — see :class:`~.api.schemas.HomeDocument`, which
    validates what arrives and is deliberately stricter than what leaves.

    ``modifiedAt`` is **omitted** when the document has never been edited, in
    place of inventing a stamp for it. CanopyPlus's ``HomeSettings`` decodes
    leniently and falls a missing ``modifiedAt`` back to ``.distantPast``,
    which is precisely what "never edited" means there and what keeps an
    untouched document from winning a merge against real user data. Writing
    ``.distantPast`` out explicitly would mean serialising year 1, which
    ``ISO8601DateFormatter`` is under no obligation to read back.

    Every other key is always present, nulls included; the module docstring
    says why the shelves in particular cannot be trimmed.
    """
    payload: dict[str, Any] = {
        "carouselEnabled": home.home_carousel_enabled,
        "carouselIncludeOnDeck": home.home_carousel_include_on_deck,
        "carouselShelf": home.home_carousel or upnext_shelf(),
        "homeShelves": home.home_shelves or [upnext_shelf()],
        "topShelf": home.home_top_shelf or upnext_shelf(),
    }
    if home.home_modified_at is not None:
        payload["modifiedAt"] = format_stamp(home.home_modified_at)
    return payload


def apply(home: HomeSettingsLike, incoming: HomeDocument) -> None:
    """Write a validated :class:`~.api.schemas.HomeDocument` onto ``home``.

    Takes the client's own ``modifiedAt`` rather than stamping a new one: the
    document is being *replicated*, not edited here, and re-stamping it would
    make every push look newer than the edit it carries and defeat the merge on
    the next device to sync. The caller has already established that this
    document wins — see :mod:`.api.routes.home`.
    """
    home.home_carousel_enabled = incoming.carouselEnabled
    home.home_carousel_include_on_deck = incoming.carouselIncludeOnDeck
    home.home_carousel = incoming.carouselShelf.model_dump()
    home.home_shelves = [shelf.model_dump() for shelf in incoming.homeShelves]
    home.home_top_shelf = incoming.topShelf.model_dump()
    home.home_modified_at = as_utc(incoming.modifiedAt)


__all__ = [
    "WIRE_TIMESTAMP_FORMAT",
    "HomeSettingsLike",
    "apply",
    "as_utc",
    "document",
    "effective_home",
    "format_stamp",
    "get_or_create_home",
    "get_own_home",
    "now_stamp",
    "seeded",
    "touched",
]
