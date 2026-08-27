"""Action CRUD.

**Every action's name and button title are the admin's to change, the built-in
Request action included.** Nothing identifies an action by its name: the server
finds the built-in one by ``is_system`` and the client tells a request button
from a grab button by the ``kind`` field in the actions payload. So a name is a
label and nothing else, and an admin who wants their Request button filed as
"Ask the household" may have it.

One word is held back, and for a reason about meaning rather than machinery:
"Request" is what this service calls filing a request in Seerr, so only the
built-in action may be named or titled it. A grab button labelled *Request*
would tell a user it was going to do the one thing it cannot. Nothing breaks if
it happens — no lookup consults the word — it is simply a lie to whoever is
holding the remote, so the admin UI declines to write it.

What the built-in action still refuses is a download client or a quality
profile — it never touches Prowlarr, so there is nothing for either to mean —
and deletion, because it is the only route to ``POST /request`` and the next
startup would seed it straight back anyway.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ....bootstrap import REQUEST_ACTION_NAME
from ....db.models import Action, QualityProfile
from ....db.session import get_config
from ....prowlarr.client import ProwlarrClient, ProwlarrError
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions", tags=["admin"])


async def _download_clients(state: StateDep, db: DbDep) -> tuple[list[dict], str | None]:
    config = await get_config(db)
    if not config.prowlarr_url or not config.prowlarr_api_key:
        return [], "Prowlarr is not configured yet."
    prowlarr = ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key, client=state.http)
    try:
        return [
            {"id": c.id, "name": c.name, "enable": c.enable, "protocol": c.protocol}
            for c in await prowlarr.list_download_clients()
        ], None
    except ProwlarrError as exc:
        return [], str(exc)


#: Matches ``Action.display_title``'s column width; the form is client input.
MAX_DISPLAY_TITLE = 128


def _clean_display_title(raw: str) -> str | None:
    """Normalise the button-copy field. Blank means "use the name"."""
    clean = raw.strip()
    if not clean:
        return None
    if len(clean) > MAX_DISPLAY_TITLE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A button title can be at most {MAX_DISPLAY_TITLE} characters.",
        )
    return clean


def _reject_reserved_word(value: str | None, *, field: str) -> None:
    """Keep "Request", however capitalised, to the built-in action.

    Matched on the whole label rather than as a substring: "Request" is the
    claim being reserved, while "Request in 4K" or "Requested" are an admin's
    own words and none of our business.
    """
    if value is None or value.casefold() != REQUEST_ACTION_NAME.casefold():
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"'{REQUEST_ACTION_NAME}' belongs to the built-in action — it means"
        " filing a request in Seerr, which this action cannot do. Pick another"
        f" {field}.",
    )


async def _load(db: DbDep, action_id: int) -> Action:
    action = await db.get(Action, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such action")
    return action


@router.get("", response_class=HTMLResponse)
async def list_actions(
    request: Request, state: StateDep, db: DbDep, admin: AdminPageDep
) -> Response:
    actions = list(
        (await db.execute(select(Action).order_by(Action.is_system, Action.name)))
        .scalars()
        .all()
    )
    profiles = list(
        (await db.execute(select(QualityProfile).order_by(QualityProfile.name)))
        .scalars()
        .all()
    )
    clients, client_error = await _download_clients(state, db)
    client_names = {client["id"]: client["name"] for client in clients}

    return templates.TemplateResponse(
        request,
        "actions.html",
        {
            "actions": actions,
            "profiles": profiles,
            "clients": clients,
            "client_names": client_names,
            "client_error": client_error,
            "admin": admin,
            "title": "Actions",
            "nav": "actions",
        },
    )


@router.post("")
async def create_action(
    db: DbDep,
    admin: AdminPageDep,
    name: str = Form(...),
    download_client_id: int = Form(...),
    quality_profile_id: int = Form(...),
    display_title: str = Form(default=""),
) -> Response:
    clean = name.strip()
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An action needs a name.")
    clean_title = _clean_display_title(display_title)
    _reject_reserved_word(clean, field="name")
    _reject_reserved_word(clean_title, field="button title")
    if await db.get(QualityProfile, quality_profile_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such quality profile")

    db.add(
        Action(
            name=clean,
            display_title=clean_title,
            download_client_id=download_client_id,
            quality_profile_id=quality_profile_id,
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"An action named '{clean}' already exists."
        ) from exc

    return RedirectResponse("/admin/actions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{action_id}")
async def update_action(
    db: DbDep,
    admin: AdminPageDep,
    action_id: int,
    name: str = Form(...),
    display_title: str = Form(default=""),
    download_client_id: int | None = Form(default=None),
    quality_profile_id: int | None = Form(default=None),
) -> Response:
    """Rename an action, retitle its button, and repoint it.

    One endpoint for both kinds, because the labels work identically on both.
    The Prowlarr targets are optional here rather than required: the built-in
    action's row has no such fields to submit, and offering it a download client
    would be offering it something it can never use.
    """
    action = await _load(db, action_id)
    clean = name.strip()
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An action needs a name.")
    clean_title = _clean_display_title(display_title)

    if action.is_system:
        # The reserved word is its own — this is the action the word describes.
        if download_client_id is not None or quality_profile_id is not None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"'{action.name}' files a request in Seerr and never touches"
                " Prowlarr, so it takes no download client or quality profile.",
            )
    else:
        _reject_reserved_word(clean, field="name")
        _reject_reserved_word(clean_title, field="button title")
        # Guaranteed by ck_action_targets_required_unless_system, and worth a
        # readable error rather than an IntegrityError from the flush.
        if download_client_id is None or quality_profile_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "An action needs a download client and a quality profile.",
            )
        if await db.get(QualityProfile, quality_profile_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such quality profile")

    # Validated first, applied second: raising mid-update would roll the
    # request's transaction back anyway, but a half-applied action is not a
    # state worth being able to reason about.
    action.name = clean
    action.display_title = clean_title
    if not action.is_system:
        action.download_client_id = download_client_id
        action.quality_profile_id = quality_profile_id

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"An action named '{clean}' already exists."
        ) from exc

    return RedirectResponse("/admin/actions", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{action_id}/delete")
async def delete_action(db: DbDep, admin: AdminPageDep, action_id: int) -> Response:
    action = await _load(db, action_id)
    if action.is_system:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{action.name}' is the built-in request action and cannot be deleted"
            " — the next start would seed it again. Revoke it per user on the"
            " Permissions page instead.",
        )
    await db.delete(action)
    return RedirectResponse("/admin/actions", status_code=status.HTTP_303_SEE_OTHER)
