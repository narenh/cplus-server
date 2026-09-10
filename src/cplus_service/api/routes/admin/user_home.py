"""One user's own Home — Carousel, Top Shelf and Shelves, editable per-user.

Reached from the Permissions page (``/admin/users``), which links each row to
``/admin/users/{id}/home``. This is the exact same editing surface as the
Libraries & Home tab's own Home section (:mod:`.libraries`) — same partials,
same live-apply behaviour, same content-source menu — pointed at one user's
own :class:`~cplus_service.db.models.UserHomeSettings` row instead of the
admin's global :class:`~cplus_service.db.models.Config`. See
:mod:`.shelf_rows` for the shared plumbing both modules build on.

**A user's Home is their own copy, not a view onto the default.** The first
time anyone opens this page for a user, :func:`_get_or_create_home` seeds a
full copy from the admin's current global defaults — after that, editing a
user's shelf never touches ``Config``, and a later change to the global
default never touches this user. Seeding itself leaves ``home_modified_at``
unset (see :class:`~cplus_service.db.models.UserHomeSettings`); every
mutating route below calls :func:`.shelf_rows.touched` once it has actually
changed something, the same single whole-document stamp
:mod:`.libraries` keeps on ``Config``. There is deliberately no "reset to
default" here yet: once a client actually syncs against CanopyPlus's own
per-user ``HomeSettings``, this row already speaks its exact shape — same
five content fields, one ``modifiedAt`` for the document — so there is
nothing left to translate when that day comes.

Default Libraries — which Plex libraries exist and what they're called — is
not part of this: every user's shelves still pick from the admin's own
:attr:`Config.default_libraries` and the admin's own Plex connection, the
same way :mod:`.shelf_rows` already assumes for the global page. Only the
shelf-shaped content itself (the shelf list, the carousel, the top shelf) is
per-user.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ....bootstrap import upnext_shelf
from ....db.models import Config, User, UserHomeSettings
from ....db.session import get_config
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
    touched,
)


async def _target_user(db: DbDep, user_id: int) -> User:
    """The user whose Home this page/action is for, or a 404.

    A path-parameter dependency, same shape as every other admin route that
    takes a resource id — resolved once per request rather than repeated in
    every handler below.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
    return user


TargetUserDep = Annotated[User, Depends(_target_user)]

router = APIRouter(prefix="/users/{user_id}/home", tags=["admin"])


def _base_url(user_id: int) -> str:
    """Where every URL :mod:`.shelf_rows`'s context builders hand to a
    template is rooted, for this one user's Home. See ``libraries.HOME_BASE_URL``
    for the admin's own global equivalent.
    """
    return f"/admin/users/{user_id}/home"


async def _get_or_create_home(db: AsyncSession, user: User, config: Config) -> UserHomeSettings:
    """This user's own Home settings, seeded from the admin's current global
    defaults the first time anyone opens their editor.

    Mirrors ``db.session.get_config``'s own get-or-create shape. Seeding
    happens exactly once, at creation — see the module docstring for why
    this deliberately never re-seeds from ``Config`` afterwards. The same
    "fall back to a fresh Continue Watching shelf" rule ``defaults_payload``
    already applies to the global config's own gaps covers a global config
    that has never had a Carousel, Top Shelf or shelf list configured
    either — the seed is never itself empty. ``home_modified_at`` is left
    unset: seeding is not an edit, the same way CanopyPlus's own
    ``HomeSettings()`` starts at ``.distantPast`` until a person actually
    changes something.
    """
    home = await db.get(UserHomeSettings, user.id)
    if home is not None:
        return home

    home = UserHomeSettings(
        user_id=user.id,
        home_shelves=[dict(shelf) for shelf in config.home_shelves] or [upnext_shelf()],
        home_carousel=dict(config.home_carousel) if config.home_carousel else upnext_shelf(),
        home_carousel_enabled=config.home_carousel_enabled,
        home_carousel_include_on_deck=config.home_carousel_include_on_deck,
        home_top_shelf=dict(config.home_top_shelf) if config.home_top_shelf else upnext_shelf(),
    )
    db.add(home)
    await db.flush()
    return home


async def _home_shelves_section(
    request: Request, db: DbDep, state: AppState, user: User
) -> Response:
    """The Shelves list alone, for every htmx write to it."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    return templates.TemplateResponse(
        request,
        "partials/home_shelves.html",
        await shelves_context(home, config, state, base_url=_base_url(user.id)),
    )


async def _carousel_section(request: Request, db: DbDep, state: AppState, user: User) -> Response:
    """The Carousel section alone, for every htmx write to it."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    return templates.TemplateResponse(
        request,
        "partials/carousel.html",
        {"carousel": await carousel_context(home, config, state, base_url=_base_url(user.id))},
    )


async def _top_shelf_section(request: Request, db: DbDep, state: AppState, user: User) -> Response:
    """The Top Shelf section alone, for every htmx write to it."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    return templates.TemplateResponse(
        request,
        "partials/top_shelf.html",
        {"top_shelf": await top_shelf_context(home, config, state, base_url=_base_url(user.id))},
    )


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def user_home_page(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, user: TargetUserDep
) -> Response:
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    return templates.TemplateResponse(
        request,
        "user_home.html",
        {
            **await home_context(home, config, state, base_url=_base_url(user.id)),
            "target_user": user,
            "admin": admin,
            "title": f"{user.plex_username}'s Home",
            "nav": "users",
        },
    )


# --------------------------------------------------------------------------- #
# Shelves
# --------------------------------------------------------------------------- #


@router.post("/shelves", response_class=HTMLResponse)
async def add_shelf(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, user: TargetUserDep
) -> Response:
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_shelves = [*home.home_shelves, upnext_shelf()]
    touched(home)
    return await _home_shelves_section(request, db, state, user)


@router.post("/shelves/reorder", response_class=HTMLResponse)
async def reorder_shelves(
    request: Request, db: DbDep, state: StateDep, admin: AdminPageDep, user: TargetUserDep
) -> Response:
    form = await request.form()
    order = [str(value) for value in form.getlist("order")]
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_shelves = reordered(home.home_shelves, order)
    touched(home)
    return await _home_shelves_section(request, db, state, user)


@router.post("/shelves/{shelf_id}/move-up", response_class=HTMLResponse)
async def move_shelf_up(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    shelf_id: str,
) -> Response:
    """Swap this shelf with the one before it. See :func:`.shelf_rows.moved`."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_shelves = moved(home.home_shelves, shelf_id, -1)
    touched(home)
    return await _home_shelves_section(request, db, state, user)


@router.post("/shelves/{shelf_id}/move-down", response_class=HTMLResponse)
async def move_shelf_down(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    shelf_id: str,
) -> Response:
    """Swap this shelf with the one after it. See :func:`.shelf_rows.moved`."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_shelves = moved(home.home_shelves, shelf_id, 1)
    touched(home)
    return await _home_shelves_section(request, db, state, user)


@router.post("/shelves/{shelf_id}/remove", response_class=HTMLResponse)
async def remove_shelf(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    shelf_id: str,
) -> Response:
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    if len(home.home_shelves) <= 1:
        # Unreachable through the page — the button is disabled — but this
        # user's Home should never be left with an empty shelf list any
        # more than the admin's global default is.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Keep at least one home shelf.")

    home.home_shelves = [shelf for shelf in home.home_shelves if shelf["id"] != shelf_id]
    touched(home)
    return await _home_shelves_section(request, db, state, user)


@router.post("/shelves/{shelf_id}", response_class=HTMLResponse)
async def update_shelf(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    shelf_id: str,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save one shelf's edits. See :func:`.shelf_rows.apply_shelf_update`."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    updated = []
    found = False
    for shelf in home.home_shelves:
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

    home.home_shelves = updated
    touched(home)
    return await _home_shelves_section(request, db, state, user)


# --------------------------------------------------------------------------- #
# Carousel
# --------------------------------------------------------------------------- #


@router.post("/carousel", response_class=HTMLResponse)
async def update_carousel(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save the Carousel's edits. Same fields, same rules as :func:`update_shelf`."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    current = home.home_carousel or upnext_shelf()
    home.home_carousel = await apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    touched(home)
    return await _carousel_section(request, db, state, user)


@router.post("/carousel-enabled", response_class=HTMLResponse)
async def toggle_carousel_enabled(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    enabled: str = Form(default=""),
) -> Response:
    """Show or hide this user's Carousel row. Never touches ``home_carousel`` itself."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_carousel_enabled = enabled == "on"
    touched(home)
    return await _carousel_section(request, db, state, user)


@router.post("/carousel-include-on-deck", response_class=HTMLResponse)
async def toggle_carousel_include_on_deck(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    enabled: str = Form(default=""),
) -> Response:
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    home.home_carousel_include_on_deck = enabled == "on"
    touched(home)
    return await _carousel_section(request, db, state, user)


# --------------------------------------------------------------------------- #
# Top Shelf
# --------------------------------------------------------------------------- #


@router.post("/top-shelf", response_class=HTMLResponse)
async def update_top_shelf(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user: TargetUserDep,
    source: str = Form(...),
    title: str = Form(default=""),
    style: str = Form(default="poster"),
    title_only: str = Form(default=""),
    collection_title: str = Form(default=""),
) -> Response:
    """Save the Top Shelf's edits. Same fields, same rules as :func:`update_shelf`."""
    config = await get_config(db)
    home = await _get_or_create_home(db, user, config)
    current = home.home_top_shelf or upnext_shelf()
    home.home_top_shelf = await apply_shelf_update(
        config,
        state,
        current,
        source=source,
        title=title,
        style=style,
        title_only=title_only,
        collection_title=collection_title,
    )
    touched(home)
    return await _top_shelf_section(request, db, state, user)
