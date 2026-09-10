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
  The row-by-row plumbing (context building, applying an edit, reordering)
  lives in :mod:`.shelf_rows`, shared with :mod:`.user_home`'s per-user
  version of this same section.

**Everything here writes in place, with no page reload.** Every add, rename,
remove, reorder and content edit posts via htmx and gets back the whole card
or row group it changed (``partials/library_section.html``,
``partials/home_shelves.html``, ``partials/carousel.html``,
``partials/top_shelf.html``), swapped in by id — the same pattern
``partials/notification_panel.html`` uses. Dragging is live
(``static/reorder.js`` moves rows as you drag); dropping posts the new
order. Shelves also get plain Move Up/Move Down buttons alongside dragging
(see :func:`.shelf_rows.moved`) — a form POST needs no native HTML5
drag-and-drop support, which not every browser or input device offers. A
row's Save control only shows once its Title actually differs from what it
started at (``static/dirty-save.js``) — every other field applies the
moment it changes (``static/shelf-source.js``), the same way a switch
elsewhere in this admin UI does.

Home shelves, the Carousel and the Top Shelf are all "shelf-shaped" — one
``HomeShelfDataModel``-equivalent dict apiece — and share one row template
(``partials/_shelf_row_fields.html``) and one update code path
(:func:`.shelf_rows.apply_shelf_update`) for exactly that reason: a
content-source edit means the same thing regardless of which of the three it
lands on. They differ only in whether there can be more than one (shelves:
yes, reorderable and removable; Carousel/Top Shelf: no) and whether it can be
switched off (Carousel: yes, via its own enabled flag, which just dims the
row rather than clearing it — see ``partials/carousel.html``; Top Shelf:
never, because tvOS itself always shows *something* there).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from ....auth.identity import refresh_plex_server
from ....bootstrap import upnext_shelf
from ....db.models import Config
from ....db.session import get_config
from ....home import touched
from ....plex.client import PlexServerClient, PlexServerError
from ....web import templates
from ...deps import DbDep, StateDep
from ...state import AppState
from .deps import AdminPageDep
from .shelf_rows import (
    apply_shelf_update,
    carousel_context,
    home_context,
    moved,
    reordered,
    shelves_context,
    top_shelf_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/libraries", tags=["admin"])

#: Where every Home URL :mod:`.shelf_rows`'s context builders hand to a
#: template is rooted, for the admin's own global default. See
#: :mod:`.user_home` for the per-user equivalent.
HOME_BASE_URL = "/admin/libraries/home"

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


# --------------------------------------------------------------------------- #
# Home: Shelves
# --------------------------------------------------------------------------- #


async def _home_shelves_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Shelves list alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "partials/home_shelves.html",
        await shelves_context(config, config, state, base_url=HOME_BASE_URL),
    )


async def _carousel_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Carousel section alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "partials/carousel.html",
        {"carousel": await carousel_context(config, config, state, base_url=HOME_BASE_URL)},
    )


async def _top_shelf_section(request: Request, db: DbDep, state: AppState) -> Response:
    """The Top Shelf section alone, for every htmx write to it."""
    config = await get_config(db)
    return templates.TemplateResponse(
        request,
        "partials/top_shelf.html",
        {"top_shelf": await top_shelf_context(config, config, state, base_url=HOME_BASE_URL)},
    )


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


async def _page_context(db: DbDep, state: AppState) -> dict[str, object]:
    library_ctx = await _library_context(db, state)
    config = await get_config(db)
    home_ctx = await home_context(config, config, state, base_url=HOME_BASE_URL)
    return {**library_ctx, **home_ctx}


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
    config.default_libraries = reordered(config.default_libraries, order)
    return await _library_section(request, db, state)


# --------------------------------------------------------------------------- #
# Home shelves
# --------------------------------------------------------------------------- #


@router.post("/home/shelves", response_class=HTMLResponse)
async def add_shelf(request: Request, db: DbDep, state: StateDep, admin: AdminPageDep) -> Response:
    config = await get_config(db)
    config.home_shelves = [*config.home_shelves, upnext_shelf()]
    touched(config)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/reorder", response_class=HTMLResponse)
async def reorder_shelves(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep
) -> Response:
    form = await request.form()
    order = [str(value) for value in form.getlist("order")]
    config = await get_config(db)
    config.home_shelves = reordered(config.home_shelves, order)
    touched(config)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}/move-up", response_class=HTMLResponse)
async def move_shelf_up(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, shelf_id: str
) -> Response:
    """Swap this shelf with the one before it. See :func:`.shelf_rows.moved`."""
    config = await get_config(db)
    config.home_shelves = moved(config.home_shelves, shelf_id, -1)
    touched(config)
    return await _home_shelves_section(request, db, state)


@router.post("/home/shelves/{shelf_id}/move-down", response_class=HTMLResponse)
async def move_shelf_down(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, shelf_id: str
) -> Response:
    """Swap this shelf with the one after it. See :func:`.shelf_rows.moved`."""
    config = await get_config(db)
    config.home_shelves = moved(config.home_shelves, shelf_id, 1)
    touched(config)
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
    touched(config)
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
    """Save one shelf's edits. See :func:`.shelf_rows.apply_shelf_update`."""
    config = await get_config(db)
    updated = []
    found = False
    for shelf in config.home_shelves:
        if shelf["id"] != shelf_id:
            updated.append(shelf)
            continue
        found = True
        updated.append(
            await apply_shelf_update(
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
    touched(config)
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
    config.home_carousel = await apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    touched(config)
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
    touched(config)
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
    touched(config)
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
    config.home_top_shelf = await apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    touched(config)
    return await _top_shelf_section(request, db, state)
