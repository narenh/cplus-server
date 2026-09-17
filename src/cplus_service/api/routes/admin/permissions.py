"""Per-user permission toggles.

Users appear here once they have signed in from a client at least once —
identity is owned by Seerr and rows are created lazily by ``GET /register``, so
there is nobody to grant access to before that.

Revoking takes effect at the user's next ``/register`` call rather than
immediately, because ``/titles/{imdb_id}/actions``, ``/search`` and ``/grab``
authenticate from the stored Plex-token mapping. That is the accepted tradeoff
from stage 2, and the page says so rather than pretending otherwise. Deleting a
user *is* immediate: it drops their stored tokens and browser sessions on the
spot.

Both writes here land in place, with no page reload: a toggle swaps its own
checkbox (``partials/permission_cell.html``), a removal swaps the whole list
(``partials/users_list.html``), since every card below the removed one moves up.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ....auth.sessions import destroy_sessions_for_user
from ....db.models import Action, Permission, User
from ....web import templates
from ...deps import DbDep
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin"])


async def _list_context(db: DbDep) -> dict[str, object]:
    """Everything the user list renders from.

    Shared by the full page and by the partial a removal swaps back in, so the
    two cannot drift into showing different grants for the same user.
    """
    users = list(
        (await db.execute(select(User).order_by(User.seerr_user_id))).scalars().all()
    )
    # The ranking, the same order GET /admin/actions lists them in and the same
    # order the user's own client draws them: an admin deciding what someone may
    # do should be reading the list that person will see, not an alphabetical
    # one that agrees with no other screen.
    actions = list(
        (await db.execute(select(Action).order_by(Action.sort_order, Action.id)))
        .scalars()
        .all()
    )
    granted = {
        (row.user_id, row.action_id)
        for row in (await db.execute(select(Permission))).scalars().all()
    }
    return {"users": users, "actions": actions, "granted": granted}


@router.get("", response_class=HTMLResponse)
async def list_users(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    return templates.TemplateResponse(
        request,
        "permissions.html",
        {
            **await _list_context(db),
            "admin": admin,
            "title": "Users",
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


@router.post("/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    request: Request, db: DbDep, admin: AdminPageDep, user_id: int
) -> Response:
    """Remove a user, revoking their access immediately rather than at next launch.

    Answers with the list rather than a redirect: the card goes away in place,
    the same way every other write in this admin UI lands.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")

    # Deleting the user cascades to their stored Plex-token mappings and to
    # their browser sessions, so access ends immediately rather than at the
    # user's next launch.
    await destroy_sessions_for_user(db, user_id)
    await db.delete(user)
    await db.flush()

    return templates.TemplateResponse(
        request, "partials/users_list.html", await _list_context(db)
    )
