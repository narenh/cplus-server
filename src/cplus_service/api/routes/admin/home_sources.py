"""Shared "content source" encoding for home shelves and the carousel.

Mirrors CanopyPlus's own ``HomeShelfPathMenu`` exactly: each option there sets
a shelf's ``path``, ``discoverHubKey`` and ``description`` — none of which are
otherwise editable — plus starting values for ``title``, ``style`` and
``titleOnly``, which an admin can still change afterwards, same as in the app.

One entry from that menu is deliberately not offered here: "Collection
Items…", which lists Plex collections by name within a library. That is a
second live Plex call and a second-level dropdown for a single row in a
picker most admins will not need; there is no substitute for it today.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import Any

ON_DECK = "ondeck"
DISCOVER_WATCHLIST = "discover:watchlist"
DISCOVER_TRENDING = "discover:trending"

WATCHLIST_HUB_KEY = "/library/sections/watchlist/all"
TRENDING_HUB_KEY = "/hubs/sections/home/trending-plex"
ON_DECK_PATH = "/library/onDeck"


@dataclass(frozen=True)
class SourceOption:
    """One entry in a "content source" dropdown."""

    value: str
    label: str
    group: str | None = None


def _library_label(library: dict[str, Any]) -> str:
    return str(library.get("name") or library.get("serverTitle") or library.get("id", ""))


def source_options(
    libraries: list[dict[str, Any]], *, allow_discover: bool
) -> list[SourceOption]:
    """Every source an admin may pick, in the app's own menu order.

    ``allow_discover`` mirrors ``HomeShelfPathMenu(allowDiscover:)`` — ordinary
    shelves offer the Discover Hubs group, the carousel does not.
    """
    options = [SourceOption(value=ON_DECK, label="Continue Watching")]
    for library in libraries:
        group = _library_label(library)
        library_id = library["id"]
        options.extend(
            [
                SourceOption(f"lib:{library_id}:newest", "Recently Released", group),
                SourceOption(f"lib:{library_id}:recentlyAdded", "Recently Added", group),
                SourceOption(f"lib:{library_id}:collections", "All Collections", group),
                SourceOption(f"lib:{library_id}:all", "All Items", group),
            ]
        )
    if allow_discover:
        options.extend(
            [
                SourceOption(DISCOVER_WATCHLIST, "Plex Watchlist", "Discover Hubs"),
                SourceOption(DISCOVER_TRENDING, "Trending on Plex", "Discover Hubs"),
            ]
        )
    return options


def grouped_options(
    options: list[SourceOption],
) -> list[tuple[str | None, list[SourceOption]]]:
    """``options`` bucketed by ``group``, for an ``<optgroup>`` per library.

    A plain ``itertools.groupby`` rather than Jinja's ``groupby`` filter, which
    sorts by the group key first — and sorting ``None`` against a library's
    name raises in Python 3. :func:`source_options` already emits each group's
    entries contiguously, so no sort is needed at all.
    """
    return [(key, list(items)) for key, items in groupby(options, key=lambda o: o.group)]


def resolve_source(
    source: str, libraries_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """The shelf fields one menu tap sets, or ``None`` for an unrecognised source."""
    if source == ON_DECK:
        return {
            "path": ON_DECK_PATH,
            "discoverHubKey": None,
            "description": "Continue Watching / On Deck",
            "title": "Continue Watching",
            "style": "card",
            "titleOnly": False,
        }
    if source == DISCOVER_WATCHLIST:
        return {
            "path": "",
            "discoverHubKey": WATCHLIST_HUB_KEY,
            "description": "Plex Watchlist",
            "title": "Plex Watchlist",
            "style": "poster",
            "titleOnly": False,
        }
    if source == DISCOVER_TRENDING:
        return {
            "path": "",
            "discoverHubKey": TRENDING_HUB_KEY,
            "description": "Trending on Plex",
            "title": "Trending",
            "style": "poster",
            "titleOnly": False,
        }

    if not source.startswith("lib:"):
        return None
    try:
        _, library_id, kind = source.split(":", 2)
    except ValueError:
        return None
    library = libraries_by_id.get(library_id)
    if library is None:
        return None

    server_title = str(library.get("serverTitle", ""))
    name = str(library.get("name", server_title))
    poster_by_default = library.get("type") == "movie"

    if kind == "newest":
        return {
            "path": f"/library/sections/{library_id}/newest",
            "discoverHubKey": None,
            "description": f"{server_title}: Recently Released",
            "title": f"Recently Released {name}",
            "style": "poster" if poster_by_default else "card",
            "titleOnly": True,
        }
    if kind == "recentlyAdded":
        return {
            "path": f"/library/sections/{library_id}/recentlyAdded",
            "discoverHubKey": None,
            "description": f"{server_title}: Recently Added",
            "title": f"Recently Added {name}",
            "style": "poster" if poster_by_default else "card",
            "titleOnly": False,
        }
    if kind == "collections":
        return {
            "path": f"/library/sections/{library_id}/collections",
            "discoverHubKey": None,
            "description": f"{server_title}: All Collections",
            "title": f"Collections in {name}",
            "style": "poster",
            "titleOnly": True,
        }
    if kind == "all":
        return {
            "path": f"/library/sections/{library_id}/all",
            "discoverHubKey": None,
            "description": f"{server_title}: All Items",
            "title": f"All {name}",
            "style": "poster",
            "titleOnly": False,
        }
    return None


def source_of(shelf: dict[str, Any]) -> str:
    """The source value that reconstructs ``shelf``'s own path — pre-selects the dropdown."""
    hub = shelf.get("discoverHubKey")
    if hub == WATCHLIST_HUB_KEY:
        return DISCOVER_WATCHLIST
    if hub == TRENDING_HUB_KEY:
        return DISCOVER_TRENDING

    path = shelf.get("path") or ""
    if path == ON_DECK_PATH:
        return ON_DECK

    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[0] == "library" and parts[1] == "sections":
        return f"lib:{parts[2]}:{parts[3]}"
    return ""
