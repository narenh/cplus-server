"""The Libraries & Home tab.

Two things live here, both stored on the ``Config`` singleton in exactly the
shape CanopyPlus's own Codable structs encode to (``MediaLibrary`` and
``HomeShelfDataModel``), so a future connection between the two sides has
nothing to translate:

* **Default Libraries** — an ordered, renameable subset of the admin's own
  Plex libraries. Renaming here is purely cosmetic: it changes what Canopy+
  displays and never touches the library's name in Plex itself. This is the
  one page in the admin webui that talks to the Plex Media Server directly
  rather than through Seerr — see :mod:`cplus_service.plex.client` for why,
  and ``Config.plex_admin_token`` for where the credential comes from.

* **Home** — the Carousel, the Top Shelf, and the shelves a fresh install
  shows on its Home tab, all built from the same "content source" menu
  CanopyPlus itself offers when editing a shelf. See :mod:`.home_sources`.

**Everything here writes in place, with no page reload.** Every add, rename,
remove, reorder and content edit posts via htmx and gets back the whole card
or row group it changed (``partials/library_section.html``,
``partials/home_shelves.html``, ``partials/carousel.html``,
``partials/top_shelf.html``), swapped in by id — the same pattern
``partials/notification_panel.html`` uses. Dragging is live
(``static/reorder.js`` moves rows as you drag); dropping posts the new
order. Shelves also get plain Move Up/Move Down buttons alongside dragging
(see :func:`_moved`) — a form POST needs no native HTML5 drag-and-drop
support, which not every browser or input device offers. A row's Save
control only shows once its Title actually differs from
what it started at (``static/dirty-save.js``) — every other field applies
the moment it changes (``static/shelf-source.js``), the same way a switch
elsewhere in this admin UI does.

Home shelves, the Carousel and the Top Shelf are all "shelf-shaped" — one
``HomeShelfDataModel``-equivalent dict apiece — and share one row template
(``partials/_shelf_row_fields.html``) and one update code path
(:func:`_apply_shelf_update`) for exactly that reason: a content-source edit
means the same thing regardless of which of the three it lands on. They
differ only in whether there can be more than one (shelves: yes, reorderable
and removable; Carousel/Top Shelf: no) and whether it can be switched off
(Carousel: yes, via its own enabled flag, which just dims the row rather
than clearing it — see ``partials/carousel.html``; Top Shelf: never, because
tvOS itself always shows *something* there).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from ....auth.identity import refresh_plex_server
from ....bootstrap import upnext_shelf
from ....db.models import Config
from ....db.session import get_config
from ....plex.client import PlexServerClient, PlexServerError
from ....web import templates
from ...deps import DbDep, StateDep
from ...state import AppState
from .deps import AdminPageDep
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/libraries", tags=["admin"])

#: CanopyPlus itself only ever fetches these three — see ``PlexServer.fetchLibraries()``,
#: which filters to exactly this set. Music and photo libraries are excluded from
#: the "add" dropdown for the same reason: there is nothing in the app that
#: would ever show one, so offering them here would just be a way to configure
#: something Canopy+ silently ignores.
SUPPORTED_LIBRARY_TYPES = {"movie", "show", "video"}

LIBRARY_TYPE_LABELS = {
    "movie": "Movies",
    "show": "TV",
    "video": "Videos",
}

PAGE_URL = "/admin/libraries"


# --------------------------------------------------------------------------- #
# Default Libraries: shared context
# --------------------------------------------------------------------------- #


async def _live_sections(config: Config, state: AppState) -> tuple[list[dict], str | None]:
    """The admin's Plex library sections Canopy+ can show, live — for the "add" dropdown.

    Returns an error message instead of raising: this page must still show
    the admin's already-configured libraries even when the server cannot be
    reached right now. Filtered to :data:`SUPPORTED_LIBRARY_TYPES` — a music
    or photo library is never offered, so the only way one ends up in
    ``default_libraries`` is a row from before this filter existed.
    """
    if not config.plex_server_base_url or not config.plex_admin_token:
        return [], "Not connected to a Plex server yet. Sign out and back in with Plex."

    plex = PlexServerClient(config.plex_server_base_url, config.plex_admin_token, client=state.http)
    try:
        sections = await plex.list_library_sections()
    except PlexServerError as exc:
        logger.warning("could not list Plex libraries: %s", exc)
        return [], f"Could not reach the Plex server: {exc}"

    return [
        {"id": section.id, "name": section.name, "type": section.type}
        for section in sections
        if section.type in SUPPORTED_LIBRARY_TYPES
    ], None


async def _library_context(db: DbDep, state: AppState) -> dict[str, object]:
    """Everything ``partials/library_section.html`` renders from.

    Shared by the full page and every write to Default Libraries, so the list
    and the "add" dropdown's available set can never drift apart — an add or
    remove is only ever seen in the same response that also updates the other.
    """
    config = await get_config(db)
    sections, plex_error = await _live_sections(config, state)

    configured_ids = {library["id"] for library in config.default_libraries}
    available = [section for section in sections if section["id"] not in configured_ids]

    return {
        "default_libraries": config.default_libraries,
        "available_sections": available,
        "library_type_labels": LIBRARY_TYPE_LABELS,
        "plex_error": plex_error,
        "plex_server_name": config.plex_server_name,
    }


async def _library_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Default Libraries card alone, for every htmx write to it."""
    return templates.TemplateResponse(
        request, "partials/library_section.html", await _library_context(db, state)
    )


def _reordered(items: list[dict], order: list[str]) -> list[dict]:
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


def _moved(items: list[dict], item_id: str, delta: int) -> list[dict]:
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
    reordered = list(items)
    reordered[index], reordered[target] = reordered[target], reordered[index]
    return reordered


# --------------------------------------------------------------------------- #
# Home: shared "shelf row" plumbing
# --------------------------------------------------------------------------- #


async def _collections_for_library(
    config: Config, state: AppState, library_id: str
) -> tuple[list[dict[str, str]], str | None]:
    """That library's own collections, live, plus an error message instead of raising.

    Shared by every "Collection Items…" picker (shelves, Carousel, Top
    Shelf) and by :func:`_first_collection_defaults`, so a page that has
    several rows pointed at the same library only asks Plex once each.
    """
    if not config.plex_server_base_url or not config.plex_admin_token:
        return [], "Not connected to a Plex server."

    plex = PlexServerClient(config.plex_server_base_url, config.plex_admin_token, client=state.http)
    try:
        collections = await plex.list_collections(library_id)
    except PlexServerError as exc:
        logger.warning("could not list collections for library %s: %s", library_id, exc)
        return [], f"Could not reach the Plex server: {exc}"
    return [{"id": c.id, "title": c.title} for c in collections], None


async def _first_collection_defaults(
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


async def _apply_shelf_update(
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
    "did the source actually change" comparison :func:`source_of` powers
    everywhere else, just extended to recognise a shelf already in
    "Collection Items…" mode as unchanged when the same library's entry is
    resubmitted (a Style or "Hide release year" edit resubmits the row's
    *whole* form, collections picker included) — without that, every such
    edit would look like a fresh switch and silently reset the chosen
    collection back to the library's first one. A genuine change mirrors
    tapping an entry in CanopyPlus's own content menu: it resets title,
    style and titleOnly to that source's defaults, discarding whatever was
    typed here, the same behaviour the app itself has.
    """
    libraries_by_id = {library["id"]: library for library in config.default_libraries}

    if source.startswith(COLLECTION_PREFIX):
        defaults = resolve_collection_source(source, collection_title, libraries_by_id)
        if defaults is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such collection.")
        return {**current, **defaults}

    parsed = collection_shelf_library_id(current, libraries_by_id)
    current_source = f"{COLLECTIONS_PREFIX}{parsed[0]}" if parsed else source_of(current)

    if source == current_source:
        return {
            **current,
            "title": title.strip() or current["title"],
            "style": style if style in ("poster", "card") else current["style"],
            "titleOnly": title_only == "on",
        }

    if source.startswith(COLLECTIONS_PREFIX):
        library_id = source[len(COLLECTIONS_PREFIX) :]
        defaults = await _first_collection_defaults(config, state, library_id)
        if defaults is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That library has no collections.")
        return {**current, **defaults}

    defaults = resolve_source(source, libraries_by_id)
    if defaults is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such content source.")
    return {**current, **defaults}


async def _collection_picker_context(
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
    collections, error = await _collections_for_library(config, state, library_id)
    return {
        "library_id": library_id,
        "collections": collections,
        "selected_collection_id": collection_id,
        "error": error,
    }


async def _row_context(
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
    the Carousel alone, its own ``extra_checkbox`` (see
    :func:`_carousel_context`) on top. ``show_title_only`` is ``False`` for
    the Carousel and Top Shelf: unlike an ordinary shelf, neither has a
    "Hide release year" setting.
    """
    libraries_by_id = {library["id"]: library for library in config.default_libraries}
    picker = await _collection_picker_context(shelf, libraries_by_id, config, state)

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
# Home: Shelves
# --------------------------------------------------------------------------- #


async def _shelves_context(config: Config, state: AppState) -> dict[str, object]:
    """Everything ``partials/home_shelves.html`` renders from."""
    rows = []
    total = len(config.home_shelves)
    for index, shelf in enumerate(config.home_shelves):
        row = await _row_context(
            shelf, config, state, allow_discover=True, show_title_only=True
        )
        row.update(
            {
                "row_id": shelf["id"],
                "post_url": f"/admin/libraries/home/shelves/{shelf['id']}",
                "remove_url": f"/admin/libraries/home/shelves/{shelf['id']}/remove",
                "move_up_url": f"/admin/libraries/home/shelves/{shelf['id']}/move-up",
                "move_down_url": f"/admin/libraries/home/shelves/{shelf['id']}/move-down",
                "swap_target": "#home-shelves",
                "show_remove": True,
                "disable_remove": total < 2,
                "is_first": index == 0,
                "is_last": index == total - 1,
            }
        )
        rows.append(row)
    return {"shelf_rows": rows}


async def _home_shelves_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Shelves list alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request, "partials/home_shelves.html", await _shelves_context(config, state)
    )


# --------------------------------------------------------------------------- #
# Home: Carousel
# --------------------------------------------------------------------------- #


async def _carousel_context(config: Config, state: AppState) -> dict[str, object]:
    """Everything ``partials/carousel.html`` renders from."""
    current = config.home_carousel or upnext_shelf()
    row = await _row_context(
        current, config, state, allow_discover=False, show_title_only=False
    )
    row.update(
        {
            "row_id": "carousel",
            "post_url": "/admin/libraries/home/carousel",
            "remove_url": None,
            "swap_target": "#carousel-section",
            "show_remove": False,
            "disable_remove": False,
            "home_carousel_enabled": config.home_carousel_enabled,
            # In the same row-2 spot "Hide release year" sits for an
            # ordinary shelf — the Carousel has no such setting, but this is
            # the one thing it has instead. Inert (and hidden) when the
            # Carousel's own source already is Continue Watching, same rule
            # as before.
            "extra_checkbox": (
                {
                    "toggle_url": "/admin/libraries/home/carousel-include-on-deck",
                    "checked": config.home_carousel_include_on_deck,
                    "label": 'Include "Continue Watching" items',
                }
                if current.get("path") != ON_DECK_PATH
                else None
            ),
        }
    )
    return row


async def _carousel_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Carousel section alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request, "partials/carousel.html", {"carousel": await _carousel_context(config, state)}
    )


# --------------------------------------------------------------------------- #
# Home: Top Shelf
# --------------------------------------------------------------------------- #


async def _top_shelf_context(config: Config, state: AppState) -> dict[str, object]:
    """Everything ``partials/top_shelf.html`` renders from."""
    current = config.home_top_shelf or upnext_shelf()
    row = await _row_context(
        current, config, state, allow_discover=False, show_title_only=False
    )
    row.update(
        {
            "row_id": "top-shelf",
            "post_url": "/admin/libraries/home/top-shelf",
            "remove_url": None,
            "swap_target": "#top-shelf-section",
            "show_remove": False,
            "disable_remove": False,
        }
    )
    return row


async def _top_shelf_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Top Shelf section alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request, "partials/top_shelf.html", {"top_shelf": await _top_shelf_context(config, state)}
    )


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


async def _home_context(config: Config, state: AppState) -> dict[str, object]:
    """Everything the Home section (Carousel, Top Shelf and shelves) renders from.

    Carousel and Top Shelf are each namespaced under their own key — they
    share every field name (both are one "shelf row"), so flattening them
    together into the same page context the way ``shelf_rows`` already is
    would have the second one silently clobber the first's.
    """
    return {
        **await _shelves_context(config, state),
        "carousel": await _carousel_context(config, state),
        "top_shelf": await _top_shelf_context(config, state),
    }


async def _page_context(db: DbDep, state: AppState) -> dict[str, object]:
    library_ctx = await _library_context(db, state)
    config = await get_config(db)
    return {**library_ctx, **await _home_context(config, state)}


@router.get("", response_class=HTMLResponse)
async def libraries_page(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    return templates.TemplateResponse(
        request,
        "libraries.html",
        {
            **await _page_context(db, state),
            "admin": admin,
            "title": "Libraries & Home",
            "nav": "libraries",
        },
    )


@router.post("/reconnect", response_class=HTMLResponse)
async def reconnect(request: Request, db: DbDep, state: StateDep, admin: AdminPageDep) -> Response:
    """Re-run Plex server discovery with the token already on file.

    The recovery path for a server that changed address, or a first attempt
    that failed transiently — same idea as the Notifications tab's own
    "Reconnect", but there is nothing to re-enrol: plex.tv is just asked again.
    """
    config = await get_config(db)
    if not config.plex_admin_token:
        return templates.TemplateResponse(
            request,
            "partials/verify.html",
            {"ok": False, "message": "No Plex sign-in on record. Sign out and back in with Plex."},
        )

    ok = await refresh_plex_server(
        config, config.plex_admin_token, config.plex_server_client_identifier or "", state.http
    )
    message = (
        f"Connected to {config.plex_server_name}."
        if ok
        else "Could not find a reachable Plex server for this account."
    )
    return templates.TemplateResponse(
        request, "partials/verify.html", {"ok": ok, "message": message}
    )


# --------------------------------------------------------------------------- #
# Default Libraries
# --------------------------------------------------------------------------- #


@router.post("", response_class=HTMLResponse)
async def add_library(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    library_id: str = Form(...),
) -> Response:
    config = await get_config(db)
    if any(library["id"] == library_id for library in config.default_libraries):
        raise HTTPException(status.HTTP_409_CONFLICT, "That library is already on the list.")

    sections, _ = await _live_sections(config, state)
    section = next((s for s in sections if s["id"] == library_id), None)
    if section is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such Plex library.")

    config.default_libraries = [
        *config.default_libraries,
        {
            "id": section["id"],
            "serverTitle": section["name"],
            "type": section["type"],
            "hidden": False,
            "name": section["name"],
        },
    ]
    return await _library_section(request, db, state)


@router.post("/{library_id}/rename", response_class=HTMLResponse)
async def rename_library(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    library_id: str,
    name: str = Form(default=""),
) -> Response:
    config = await get_config(db)
    updated = []
    found = False
    for library in config.default_libraries:
        if library["id"] == library_id:
            found = True
            clean = name.strip()
            library = {**library, "name": clean or library["serverTitle"]}
        updated.append(library)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such default library")

    config.default_libraries = updated
    return await _library_section(request, db, state)


@router.post("/{library_id}/remove", response_class=HTMLResponse)
async def remove_library(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, library_id: str
) -> Response:
    config = await get_config(db)
    config.default_libraries = [
        library for library in config.default_libraries if library["id"] != library_id
    ]
    return await _library_section(request, db, state)


@router.post("/reorder", response_class=HTMLResponse)
async def reorder_libraries(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    form = await request.form()
    order = [str(value) for value in form.getlist("order")]
    config = await get_config(db)
    config.default_libraries = _reordered(config.default_libraries, order)
    return await _library_section(request, db, state)


# --------------------------------------------------------------------------- #
# Home shelves
# --------------------------------------------------------------------------- #


@router.post("/home/shelves", response_class=HTMLResponse)
async def add_shelf(request: Request, db: DbDep, state: StateDep, admin: AdminPageDep) -> Response:
    config = await get_config(db)
    config.home_shelves = [*config.home_shelves, upnext_shelf()]
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/reorder", response_class=HTMLResponse)
async def reorder_shelves(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    form = await request.form()
    order = [str(value) for value in form.getlist("order")]
    config = await get_config(db)
    config.home_shelves = _reordered(config.home_shelves, order)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}/move-up", response_class=HTMLResponse)
async def move_shelf_up(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, shelf_id: str
) -> Response:
    """Swap this shelf with the one before it. See :func:`_moved`."""
    config = await get_config(db)
    config.home_shelves = _moved(config.home_shelves, shelf_id, -1)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}/move-down", response_class=HTMLResponse)
async def move_shelf_down(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, shelf_id: str
) -> Response:
    """Swap this shelf with the one after it. See :func:`_moved`."""
    config = await get_config(db)
    config.home_shelves = _moved(config.home_shelves, shelf_id, 1)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}/remove", response_class=HTMLResponse)
async def remove_shelf(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, shelf_id: str
) -> Response:
    config = await get_config(db)
    if len(config.home_shelves) <= 1:
        # Unreachable through the page — the button is disabled — but the app
        # itself never lets the shelf list go empty, and neither should this.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keep at least one home shelf.")

    config.home_shelves = [shelf for shelf in config.home_shelves if shelf["id"] != shelf_id]
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}", response_class=HTMLResponse)
async def update_shelf(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    shelf_id: str,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save one shelf's edits. See :func:`_apply_shelf_update`."""
    config = await get_config(db)
    updated = []
    found = False
    for shelf in config.home_shelves:
        if shelf["id"] != shelf_id:
            updated.append(shelf)
            continue
        found = True
        updated.append(
            await _apply_shelf_update(
                config,
                state,
                shelf,
                source=source,
                title=title,
                style=style,
                title_only=title_only,
                collection_title=collection_title,
            )
        )

    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such home shelf")

    config.home_shelves = updated
    return await _home_shelves_section(request, db, state)


# --------------------------------------------------------------------------- #
# Carousel
# --------------------------------------------------------------------------- #


@router.post("/home/carousel", response_class=HTMLResponse)
async def update_carousel(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save the Carousel's edits. Same fields, same rules as :func:`update_shelf`."""
    config = await get_config(db)
    current = config.home_carousel or upnext_shelf()
    config.home_carousel = await _apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    return await _carousel_section(request, db, state)


@router.post("/home/carousel-enabled", response_class=HTMLResponse)
async def toggle_carousel_enabled(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    enabled: str = Form(default=""),
) -> Response:
    """Show or hide the Carousel row. Never touches ``home_carousel`` itself.

    Swaps in the whole Carousel section, not just the switch: whether the
    row below renders at all depends on this same flag.
    """
    config = await get_config(db)
    config.home_carousel_enabled = enabled == "on"
    return await _carousel_section(request, db, state)


@router.post("/home/carousel-include-on-deck", response_class=HTMLResponse)
async def toggle_carousel_include_on_deck(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    enabled: str = Form(default=""),
) -> Response:
    config = await get_config(db)
    config.home_carousel_include_on_deck = enabled == "on"
    return await _carousel_section(request, db, state)


# --------------------------------------------------------------------------- #
# Top Shelf
# --------------------------------------------------------------------------- #


@router.post("/home/top-shelf", response_class=HTMLResponse)
async def update_top_shelf(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save the Top Shelf's edits. Same fields, same rules as :func:`update_shelf`."""
    config = await get_config(db)
    current = config.home_top_shelf or upnext_shelf()
    config.home_top_shelf = await _apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    return await _top_shelf_section(request, db, state)
