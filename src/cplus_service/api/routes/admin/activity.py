"""Read-only grab history and activity log, filterable by user."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ....db.models import Action, ActivityLog, Grab, User
from ....tracking.state import PHASE_LABELS, GrabPhase
from ....web import templates
from ...deps import DbDep, StateDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

PAGE_SIZE = 200


async def _users(db: DbDep) -> list[User]:
    result = await db.execute(select(User).order_by(User.plex_username))
    return list(result.scalars().all())


def _tracking(grab: Grab, state: StateDep) -> dict[str, str] | None:
    """How grab tracking got on with ``grab``, for its pill; ``None`` if never tracked.

    The live state when the tracker still holds it, otherwise the row — where
    a phase that never finished means the server restarted mid-way.
    """
    live = state.grab_tracker.get(grab.id)
    if live is not None:
        phase, error = live.phase, live.error
    elif grab.tracking_phase:
        try:
            phase = GrabPhase(grab.tracking_phase)
        except ValueError:
            return None
        error = grab.tracking_error
        if not phase.finished:
            phase, error = GrabPhase.FAILED, "Interrupted by a restart."
    else:
        return None
    return {"phase": phase.value, "label": PHASE_LABELS[phase], "error": error or ""}


@router.get("/grabs", response_class=HTMLResponse)
async def list_grabs(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    user_id: int | None = Query(default=None),
) -> Response:
    query = select(Grab).order_by(Grab.created_at.desc()).limit(PAGE_SIZE)
    if user_id is not None:
        query = query.where(Grab.user_id == user_id)
    grabs = list((await db.execute(query)).scalars().all())

    users = await _users(db)
    usernames = {user.id: user.plex_username for user in users}
    action_names = {
        action.id: action.name
        for action in (await db.execute(select(Action))).scalars().all()
    }

    return templates.TemplateResponse(
        request,
        "grabs.html",
        {
            "grabs": grabs,
            "users": users,
            "usernames": usernames,
            "action_names": action_names,
            "tracking": {grab.id: t for grab in grabs if (t := _tracking(grab, state))},
            "selected_user": user_id,
            "admin": admin,
            "title": "Grabs",
            "nav": "grabs",
        },
    )


@router.get("/activity-log", response_class=HTMLResponse)
async def list_activity(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    user_id: int | None = Query(default=None),
) -> Response:
    query = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(PAGE_SIZE)
    if user_id is not None:
        query = query.where(ActivityLog.user_id == user_id)
    entries = list((await db.execute(query)).scalars().all())

    users = await _users(db)
    usernames = {user.id: user.plex_username for user in users}

    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "entries": entries,
            "users": users,
            "usernames": usernames,
            "selected_user": user_id,
            "admin": admin,
            "title": "Activity log",
            "nav": "activity",
        },
    )
