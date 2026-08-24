"""Per-user permission toggles.

Users appear here once they have signed in from a client at least once —
identity is owned by Seerr and rows are created lazily by ``GET /actions``, so
there is nobody to grant access to before that.

Revoking takes effect at the user's next ``/actions`` call rather than
immediately, because ``/search`` and ``/grab`` authenticate from the stored
Plex-token mapping. That is the accepted tradeoff from stage 2, and the page
says so rather than pretending otherwise. Deleting a user *is* immediate: it
drops their stored tokens and browser sessions on the spot.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ....auth.sessions import destroy_sessions_for_user
from ....db.models import Action, Permission, User
from ....web import templates
from ...deps import DbDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def list_users(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    users = list(
        (await db.execute(select(User).order_by(User.plex_username))).scalars().all()
    )
    actions = list(
        (await db.execute(select(Action).order_by(Action.is_system, Action.name)))
        .scalars()
        .all()
    )
    granted = {
        (row.user_id, row.action_id)
        for row in (await db.execute(select(Permission))).scalars().all()
    }

    return templates.TemplateResponse(
        request,
        "permissions.html",
        {
            "users": users,
            "actions": actions,
            "granted": granted,
            "admin": admin,
            "title": "Permissions",
            "nav": "users",
        },
    )


@router.post("/{user_id}/permissions", response_class=HTMLResponse)
async def toggle_permission(
    request: Request,
    db: DbDep,
    admin: AdminPageDep,
    user_id: int,
    action_id: int = Form(...),
    granted: str = Form(default=""),
) -> Response:
    """Grant or revoke one action for one user. Idempotent either way."""
    user = await db.get(User, user_id)
    action = await db.get(Action, action_id)
    if user is None or action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user or action")

    existing = (
        await db.execute(
            select(Permission).where(
                Permission.user_id == user_id, Permission.action_id == action_id
            )
        )
    ).scalars().first()

    should_grant = granted == "on"
    if should_grant and existing is None:
        db.add(Permission(user_id=user_id, action_id=action_id))
    elif not should_grant and existing is not None:
        await db.delete(existing)

    return templates.TemplateResponse(
        request,
        "partials/permission_cell.html",
        {
            "user": user,
            "action": action,
            "is_granted": should_grant,
        },
    )


@router.post("/{user_id}/delete")
async def delete_user(db: DbDep, admin: AdminPageDep, user_id: int) -> Response:
    """Remove a user, revoking their access immediately rather than at next launch."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")

    # Deleting the user cascades to their stored Plex-token mappings and to
    # their browser sessions, so access ends immediately rather than at the
    # user's next launch.
    await destroy_sessions_for_user(db, user_id)
    await db.delete(user)
    await db.flush()

    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
