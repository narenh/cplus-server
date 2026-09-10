"""Shared plumbing for every "shelf row": a Home shelf, the Carousel, or Top Shelf.

Factored out of :mod:`.libraries` so :mod:`.user_home` can build the exact
same rows against one user's own ``UserHomeSettings`` instead of the admin's
global ``Config`` — a content-source edit means the same thing regardless of
whose Home it lands on.

Every function here still reads library metadata and Plex credentials from
the admin's global ``Config`` regardless of whose Home is being edited — a
user's own shelf still picks from the same set of Plex libraries the admin
has configured; there is no such thing as a user's own Plex connection. Only
the shelf-shaped values themselves (the shelf list, the carousel, the top
shelf) come from whichever "home" object the caller passes in — ``Config``
itself, or one user's ``UserHomeSettings``. The two share the same field
names (``home_shelves``, ``home_carousel``, ``home_carousel_enabled``,
``home_carousel_include_on_deck``, ``home_top_shelf``) by design; see
:class:`HomeSettingsLike` and ``db.models.UserHomeSettings``.

``base_url`` is the one other thing that differs between callers —
``/admin/libraries/home`` for the global config, ``/admin/users/{id}/home``
for one user's — and every URL a context builder below hands to a template
is built from it, so ``partials/home_shelves.html``, ``partials/carousel.html``
and ``partials/top_shelf.html`` need not know which of the two they are
rendering.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import HTTPException, status

from ....bootstrap import now_iso, upnext_shelf
from ....db.models import Config
from ....plex.client import PlexServerClient, PlexServerError
from ...state import AppState
from .home_sources import (
    COLLECTION_PREFIX,
    COLLECTIONS_PREFIX,
    ON_DECK_PATH,
    collection_shelf_library_id,
    grouped_options,
    library_label,
    resolve_collection_source,
    resolve_source,
    source_of,
    source_options,
)


class HomeSettingsLike(Protocol):
    """Structural shape shared by ``Config`` and ``UserHomeSettings``.

    For type-checking only — both real classes already carry every one of
    these fields with no shared base class, since ``Config`` carries a great
    deal else that has nothing to do with Home.
    """

    home_shelves: list[dict[str, Any]]
    home_carousel: dict[str, Any] | None
    home_carousel_enabled: bool
    home_carousel_include_on_deck: bool
    home_top_shelf: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# List utilities — shared with Default Libraries' own reorder in .libraries
# --------------------------------------------------------------------------- #


def reordered(items: list[dict], order: list[str]) -> list[dict]:
    """``items`` in ``order``'s sequence, keyed by ``id``.

    Anything ``order`` left out (it should not, but the form is client input)
    keeps its relative position at the end, rather than being silently
    dropped.
    """
    by_id = {item["id"]: item for item in items}
    wanted = set(order)
    return [by_id[i] for i in order if i in by_id] + [
        item for item in items if item["id"] not in wanted
    ]


def moved(items: list[dict], item_id: str, delta: int) -> list[dict]:
    """``items`` with ``item_id`` swapped with its neighbour ``delta`` away.

    ``delta`` of ``-1`` moves it earlier, ``+1`` later. A move already at
    that end (or an unknown id) is a no-op rather than an error — the
    button that posts here is disabled in that case, but a stale click
    should do nothing, not raise. The up/down buttons this powers exist
    alongside dragging (``static/reorder.js``) rather than instead of it:
    a plain form POST needs no native HTML5 drag-and-drop support, which
    not every browser or input device offers.
    """
    index = next((i for i, item in enumerate(items) if item["id"] == item_id), None)
    if index is None:
        return items
    target = index + delta
    if not 0 <= target < len(items):
        return items
    result = list(items)
    result[index], result[target] = result[target], result[index]
    return result


# --------------------------------------------------------------------------- #
# Collections picker
# --------------------------------------------------------------------------- #


async def collections_for_library(
    config: Config, state: AppState, library_id: str
) -> tuple[list[dict[str, str]], str | None]:
    """That library's own collections, live, plus an error message instead of raising.

    Shared by every "Collection Items…" picker (shelves, Carousel, Top
    Shelf, per-user or global) and by :func:`first_collection_defaults`, so
    a page with several rows pointed at the same library only asks Plex
    once each.
    """
    if not config.plex_server_base_url or not config.plex_admin_token:
        return [], "Not connected to a Plex server."

    plex = PlexServerClient(config.plex_server_base_url, config.plex_admin_token, client=state.http)
    try:
        collections = await plex.list_collections(library_id)
    except PlexServerError as exc:
        return [], f"Could not reach the Plex server: {exc}"
    return [{"id": c.id, "title": c.title} for c in collections], None


async def first_collection_defaults(
    config: Config, state: AppState, library_id: str
) -> dict[str, Any] | None:
    """The fields a fresh switch to "Collection Items…" applies.

    Picking that entry always resolves straight to the library's own first
    collection rather than leaving the row on a picker with nothing actually
    saved yet — the second picker (already showing that collection selected)
    is what an admin then uses to change it, no differently from any other
    edit. ``None`` means the library has no collections to default to.
    """
    if not config.plex_server_base_url or not config.plex_admin_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not connected to a Plex server.")

    plex = PlexServerClient(config.plex_server_base_url, config.plex_admin_token, client=state.http)
    try:
        collections = await plex.list_collections(library_id)
    except PlexServerError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach the Plex server: {exc}"
        ) from exc
    if not collections:
        return None

    first = collections[0]
    libraries_by_id = {library["id"]: library for library in config.default_libraries}
    return resolve_collection_source(
        f"{COLLECTION_PREFIX}{library_id}:{first.id}", first.title, libraries_by_id
    )


async def collection_picker_context(
    shelf: dict[str, Any],
    libraries_by_id: dict[str, dict[str, Any]],
    config: Config,
    state: AppState,
) -> dict[str, object] | None:
    """The live collections-picker context for one shelf-shaped dict, or ``None``.

    ``None`` means ``shelf`` is not currently a "Collection Items…" shelf —
    the row renders its ordinary Content dropdown alone.
    """
    parsed = collection_shelf_library_id(shelf, libraries_by_id)
    if parsed is None:
        return None
    library_id, collection_id = parsed
    collections, error = await collections_for_library(config, state, library_id)
    return {
        "library_id": library_id,
        "collections": collections,
        "selected_collection_id": collection_id,
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Applying an edit
# --------------------------------------------------------------------------- #


def stamped(shelf_update: dict[str, Any], now_iso_value: str) -> dict[str, Any]:
    """``shelf_update`` with a fresh ``modifiedAt`` — every real edit stamps one."""
    return {**shelf_update, "modifiedAt": now_iso_value}


async def apply_shelf_update(
    config: Config,
    state: AppState,
    current: dict[str, Any],
    *,
    source: str,
    title: str,
    style: str,
    title_only: str,
    collection_title: str,
) -> dict[str, Any]:
    """The next value for one shelf-shaped dict — a Home shelf, the Carousel or Top Shelf.

    Picking a specific collection from the second picker (``source`` starts
    with ``col:``) always applies: that value only ever arrives from
    deliberately choosing an option there. Otherwise, this is the same
    "did the source actually change" comparison :func:`.home_sources.source_of`
    powers everywhere else, just extended to recognise a shelf already in
    "Collection Items…" mode as unchanged when the same library's entry is
    resubmitted (a Style or "Hide release year" edit resubmits the row's
    *whole* form, collections picker included) — without that, every such
    edit would look like a fresh switch and silently reset the chosen
    collection back to the library's first one. A genuine change mirrors
    tapping an entry in CanopyPlus's own content menu: it resets title,
    style and titleOnly to that source's defaults, discarding whatever was
    typed here, the same behaviour the app itself has.

    Every path through here is a real edit, so every path stamps a fresh
    ``modifiedAt`` on the way out — unlike reordering or removing a shelf,
    which touch the list, not any one shelf's own content.
    """
    libraries_by_id = {library["id"]: library for library in config.default_libraries}

    if source.startswith(COLLECTION_PREFIX):
        defaults = resolve_collection_source(source, collection_title, libraries_by_id)
        if defaults is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such collection.")
        return stamped({**current, **defaults}, now_iso())

    parsed = collection_shelf_library_id(current, libraries_by_id)
    current_source = f"{COLLECTIONS_PREFIX}{parsed[0]}" if parsed else source_of(current)

    if source == current_source:
        return stamped(
            {
                **current,
                "title": title.strip() or current["title"],
                "style": style if style in ("poster", "card") else current["style"],
                "titleOnly": title_only == "on",
            },
            now_iso(),
        )

    if source.startswith(COLLECTIONS_PREFIX):
        library_id = source[len(COLLECTIONS_PREFIX) :]
        defaults = await first_collection_defaults(config, state, library_id)
        if defaults is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That library has no collections.")
        return stamped({**current, **defaults}, now_iso())

    defaults = resolve_source(source, libraries_by_id)
    if defaults is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such content source.")
    return stamped({**current, **defaults}, now_iso())


# --------------------------------------------------------------------------- #
# Row context
# --------------------------------------------------------------------------- #


async def row_context(
    shelf: dict[str, Any],
    config: Config,
    state: AppState,
    *,
    allow_discover: bool,
    show_title_only: bool,
) -> dict[str, object]:
    """The picker-related fields ``partials/_shelf_row_fields.html`` needs.

    Shared by Home shelves, the Carousel and Top Shelf — each caller adds
    its own routing/identity fields (``row_id``, ``post_url``, ...) and, for
    the Carousel alone, its own ``extra_checkbox`` on top. ``show_title_only``
    is ``False`` for the Carousel and Top Shelf: unlike an ordinary shelf,
    neither has a "Hide release year" setting.
    """
    libraries_by_id = {library["id"]: library for library in config.default_libraries}
    picker = await collection_picker_context(shelf, libraries_by_id, config, state)

    if picker is not None:
        current_source = f"{COLLECTIONS_PREFIX}{picker['library_id']}"
        library = libraries_by_id.get(picker["library_id"])
        placeholder_text = library_label(library) if library else shelf["description"]
    else:
        current_source = source_of(shelf)
        placeholder_text = shelf["description"]

    return {
        "current": shelf,
        "current_source": current_source,
        "placeholder_text": placeholder_text,
        "source_groups": grouped_options(
            source_options(config.default_libraries, allow_discover=allow_discover)
        ),
        "picker": picker,
        "show_title_only": show_title_only,
        "extra_checkbox": None,
    }


# --------------------------------------------------------------------------- #
# Whole-section contexts — Shelves, Carousel, Top Shelf, and all three together
# --------------------------------------------------------------------------- #


async def shelves_context(
    home: HomeSettingsLike, config: Config, state: AppState, *, base_url: str
) -> dict[str, object]:
    """Everything ``partials/home_shelves.html`` renders from.

    ``base_url`` is ``/admin/libraries/home`` for the global default, or
    ``/admin/users/{id}/home`` for one user's own — every row and list
    action URL below is built from it.
    """
    rows = []
    total = len(home.home_shelves)
    for index, shelf in enumerate(home.home_shelves):
        row = await row_context(shelf, config, state, allow_discover=True, show_title_only=True)
        row.update(
            {
                "row_id": shelf["id"],
                "post_url": f"{base_url}/shelves/{shelf['id']}",
                "remove_url": f"{base_url}/shelves/{shelf['id']}/remove",
                "move_up_url": f"{base_url}/shelves/{shelf['id']}/move-up",
                "move_down_url": f"{base_url}/shelves/{shelf['id']}/move-down",
                "swap_target": "#home-shelves",
                "show_remove": True,
                "disable_remove": total < 2,
                "is_first": index == 0,
                "is_last": index == total - 1,
            }
        )
        rows.append(row)
    return {
        "shelf_rows": rows,
        "reorder_url": f"{base_url}/shelves/reorder",
        "add_url": f"{base_url}/shelves",
    }


async def carousel_context(
    home: HomeSettingsLike, config: Config, state: AppState, *, base_url: str
) -> dict[str, object]:
    """Everything ``partials/carousel.html`` renders from."""
    current = home.home_carousel or upnext_shelf()
    row = await row_context(current, config, state, allow_discover=False, show_title_only=False)
    row.update(
        {
            "row_id": "carousel",
            "post_url": f"{base_url}/carousel",
            "remove_url": None,
            "swap_target": "#carousel-section",
            "show_remove": False,
            "disable_remove": False,
            "home_carousel_enabled": home.home_carousel_enabled,
            "enabled_toggle_url": f"{base_url}/carousel-enabled",
            # In the same row-2 spot "Hide release year" sits for an
            # ordinary shelf — the Carousel has no such setting, but this is
            # the one thing it has instead. Inert (and hidden) when the
            # Carousel's own source already is Continue Watching, same rule
            # as before.
            "extra_checkbox": (
                {
                    "toggle_url": f"{base_url}/carousel-include-on-deck",
                    "checked": home.home_carousel_include_on_deck,
                    "label": 'Include "Continue Watching" items',
                }
                if current.get("path") != ON_DECK_PATH
                else None
            ),
        }
    )
    return row


async def top_shelf_context(
    home: HomeSettingsLike, config: Config, state: AppState, *, base_url: str
) -> dict[str, object]:
    """Everything ``partials/top_shelf.html`` renders from."""
    current = home.home_top_shelf or upnext_shelf()
    row = await row_context(current, config, state, allow_discover=False, show_title_only=False)
    row.update(
        {
            "row_id": "top-shelf",
            "post_url": f"{base_url}/top-shelf",
            "remove_url": None,
            "swap_target": "#top-shelf-section",
            "show_remove": False,
            "disable_remove": False,
        }
    )
    return row


async def home_context(
    home: HomeSettingsLike, config: Config, state: AppState, *, base_url: str
) -> dict[str, object]:
    """Everything the Home section (Carousel, Top Shelf and shelves) renders from.

    Carousel and Top Shelf are each namespaced under their own key — they
    share every field name (both are one "shelf row"), so flattening them
    together into the same page context the way ``shelf_rows`` already is
    would have the second one silently clobber the first's.
    """
    return {
        **await shelves_context(home, config, state, base_url=base_url),
        "carousel": await carousel_context(home, config, state, base_url=base_url),
        "top_shelf": await top_shelf_context(home, config, state, base_url=base_url),
    }
