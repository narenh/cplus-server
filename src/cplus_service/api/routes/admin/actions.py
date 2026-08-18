"""Action CRUD.

The built-in Request action is listed but never editable: it has no download
client and no quality profile to edit, and its name is part of the tvOS client
contract — the client routes a button to ``POST /request`` by matching on it.
Renaming or deleting it would silently break every client.
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


async def _editable(db: DbDep, action_id: int) -> Action:
    action = await db.get(Action, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such action")
    if action.is_system:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{action.name}' is built in and cannot be edited or deleted. "
            "Grant or revoke it per user on the Permissions page instead.",
        )
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
            "request_action_name": REQUEST_ACTION_NAME,
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
) -> Response:
    clean = name.strip()
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An action needs a name.")
    if clean.casefold() == REQUEST_ACTION_NAME.casefold():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{REQUEST_ACTION_NAME}' is reserved for the built-in action.",
        )
    if await db.get(QualityProfile, quality_profile_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such quality profile")

    db.add(
        Action(
            name=clean,
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
    download_client_id: int = Form(...),
    quality_profile_id: int = Form(...),
) -> Response:
    action = await _editable(db, action_id)
    clean = name.strip()
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An action needs a name.")
    if clean.casefold() == REQUEST_ACTION_NAME.casefold():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{REQUEST_ACTION_NAME}' is reserved for the built-in action.",
        )
    if await db.get(QualityProfile, quality_profile_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No such quality profile")

    action.name = clean
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
    action = await _editable(db, action_id)
    await db.delete(action)
    return RedirectResponse("/admin/actions", status_code=status.HTTP_303_SEE_OTHER)
