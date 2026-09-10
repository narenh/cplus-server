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

* **Home** — the shelves (and the one hero carousel) a fresh install shows on
  its Home tab, built from the same "content source" menu CanopyPlus itself
  offers when editing a shelf. See :mod:`.home_sources`.

**Both Default Libraries and Home shelves write in place, with no page
reload.** Every add, rename, remove and reorder posts via htmx and gets back
the whole card or list (``partials/library_section.html``,
``partials/home_shelves.html``), swapped in by id — the same pattern
``partials/notification_panel.html`` uses, and for the same reason: a list and
whatever depends on its current contents (the "add" dropdown's available set,
for libraries) are two views of one piece of state, so they are re-rendered
together or not at all. Dragging is live (``static/reorder.js`` moves rows as
you drag); dropping posts the new order. A row's Save control only shows once
one of its own fields actually differs from what it started at
(``static/dirty-save.js``).

The carousel is simpler still: its two on/off switches write through
immediately like the Notifications tab's switches, and its content-source
picker is an ordinary form post that redirects back to the page, the same
style as ``actions.py`` and ``permissions.py`` — there is exactly one of it,
so there is no list to keep in sync and no reload worth avoiding. "Reconnect"
mirrors the "Verify Prowlarr connection" button.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

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
    grouped_options,
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
# Shared context
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


def _shelves_context(config: Config) -> dict[str, object]:
    """Everything ``partials/home_shelves.html`` renders from."""
    return {
        "home_shelves": config.home_shelves,
        "shelf_source_groups": grouped_options(
            source_options(config.default_libraries, allow_discover=True)
        ),
        "source_of": source_of,
    }


def _carousel_context(config: Config) -> dict[str, object]:
    """Everything the carousel controls render from."""
    return {
        "home_carousel": config.home_carousel,
        "home_carousel_enabled": config.home_carousel_enabled,
        "home_carousel_include_on_deck": config.home_carousel_include_on_deck,
        "carousel_source_groups": grouped_options(
            source_options(config.default_libraries, allow_discover=False)
        ),
        "source_of": source_of,
    }


def _home_context(config: Config) -> dict[str, object]:
    """Everything the Home section (shelves and carousel) renders from."""
    return {**_shelves_context(config), **_carousel_context(config)}


async def _page_context(db: DbDep, state: AppState) -> dict[str, object]:
    library_ctx = await _library_context(db, state)
    config = await get_config(db)
    return {**library_ctx, **_home_context(config)}


async def _library_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Default Libraries card alone, for every htmx write to it."""
    return templates.TemplateResponse(
        request, "partials/library_section.html", await _library_context(db, state)
    )


async def _home_shelves_section(request: Request, db: DbDep) -> Response:
    """The Shelves list alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request, "partials/home_shelves.html", _shelves_context(config)
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


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


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
async def add_shelf(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    config = await get_config(db)
    config.home_shelves = [*config.home_shelves, upnext_shelf()]
    return await _home_shelves_section(request, db)


@router.post("/home/shelves/reorder", response_class=HTMLResponse)
async def reorder_shelves(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    form = await request.form()
    order = [str(value) for value in form.getlist("order")]
    config = await get_config(db)
    config.home_shelves = _reordered(config.home_shelves, order)
    return await _home_shelves_section(request, db)


@router.get("/home/shelves/{shelf_id}/collections", response_class=HTMLResponse)
async def shelf_collections(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    shelf_id: str,
    library_id: str,
) -> Response:
    """The picker of one library's own collections, for "Collection Items…".

    ``shelf_id`` names nothing here beyond which shelf's picker to wire the
    result up to — this is a read, not a write to that shelf.
    """
    config = await get_config(db)
    collections: list[dict[str, str]] = []
    error: str | None = None
    if not config.plex_server_base_url or not config.plex_admin_token:
        error = "Not connected to a Plex server."
    else:
        plex = PlexServerClient(
            config.plex_server_base_url, config.plex_admin_token, client=state.http
        )
        try:
            collections = [
                {"id": c.id, "title": c.title} for c in await plex.list_collections(library_id)
            ]
        except PlexServerError as exc:
            logger.warning("could not list collections for library %s: %s", library_id, exc)
            error = f"Could not reach the Plex server: {exc}"

    return templates.TemplateResponse(
        request,
        "partials/collections_picker.html",
        {
            "shelf_id": shelf_id,
            "library_id": library_id,
            "collections": collections,
            "error": error,
        },
    )


@router.post("/home/shelves/{shelf_id}/remove", response_class=HTMLResponse)
async def remove_shelf(request: Request, db: DbDep, admin: AdminPageDep, shelf_id: str) -> Response:
    config = await get_config(db)
    if len(config.home_shelves) <= 1:
        # Unreachable through the page — the button is disabled — but the app
        # itself never lets the shelf list go empty, and neither should this.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keep at least one home shelf.")

    config.home_shelves = [shelf for shelf in config.home_shelves if shelf["id"] != shelf_id]
    return await _home_shelves_section(request, db)


@router.post("/home/shelves/{shelf_id}", response_class=HTMLResponse)
async def update_shelf(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    shelf_id: str,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save one shelf's edits.

    Changing ``source`` mirrors tapping an entry in CanopyPlus's own content
    menu: it always resets title, style and titleOnly to that source's
    defaults, discarding whatever was typed here — the same behaviour the app
    itself has. A specific collection (``source`` starting with ``col:``) is
    the same case with a title the server has no other way to learn — see
    :func:`~.home_sources.resolve_collection_source`. Leaving the source alone
    applies the other three fields as ordinary edits, same as the app's
    separate Title field, Style picker and "Hide Release Year" toggle.
    """
    config = await get_config(db)
    libraries_by_id = {library["id"]: library for library in config.default_libraries}

    updated = []
    found = False
    for shelf in config.home_shelves:
        if shelf["id"] != shelf_id:
            updated.append(shelf)
            continue
        found = True

        if source.startswith(COLLECTION_PREFIX):
            defaults = resolve_collection_source(source, collection_title, libraries_by_id)
            if defaults is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such collection.")
            updated.append({**shelf, **defaults})
            continue

        if source != source_of(shelf):
            defaults = resolve_source(source, libraries_by_id)
            if defaults is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such content source.")
            updated.append({**shelf, **defaults})
            continue

        updated.append(
            {
                **shelf,
                "title": title.strip() or shelf["title"],
                "style": style if style in ("poster", "card") else shelf["style"],
                "titleOnly": title_only == "on",
            }
        )

    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such home shelf")

    config.home_shelves = updated
    return await _home_shelves_section(request, db)


# --------------------------------------------------------------------------- #
# Carousel
# --------------------------------------------------------------------------- #


@router.post("/home/carousel")
async def update_carousel(db: DbDep, admin: AdminPageDep, source: str = Form(...)) -> Response:
    config = await get_config(db)

    if source == "off":
        config.home_carousel = None
        return RedirectResponse(PAGE_URL, status_code=status.HTTP_303_SEE_OTHER)

    libraries_by_id = {library["id"]: library for library in config.default_libraries}
    defaults = resolve_source(source, libraries_by_id)
    if defaults is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such content source.")

    config.home_carousel = {"id": str(uuid.uuid4()), **defaults}
    return RedirectResponse(PAGE_URL, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/home/carousel-enabled", response_class=HTMLResponse)
async def toggle_carousel_enabled(
    request: Request, db: DbDep, admin: AdminPageDep, enabled: str = Form(default="")
) -> Response:
    config = await get_config(db)
    config.home_carousel_enabled = enabled == "on"
    return templates.TemplateResponse(
        request,
        "partials/carousel_switch.html",
        {
            "checked": config.home_carousel_enabled,
            "url": "/admin/libraries/home/carousel-enabled",
            "label": "Enable Home Carousel",
        },
    )


@router.post("/home/carousel-include-on-deck", response_class=HTMLResponse)
async def toggle_carousel_include_on_deck(
    request: Request, db: DbDep, admin: AdminPageDep, enabled: str = Form(default="")
) -> Response:
    config = await get_config(db)
    config.home_carousel_include_on_deck = enabled == "on"
    return templates.TemplateResponse(
        request,
        "partials/carousel_switch.html",
        {
            "checked": config.home_carousel_include_on_deck,
            "url": "/admin/libraries/home/carousel-include-on-deck",
            "label": 'Include "Continue Watching" items',
        },
    )
