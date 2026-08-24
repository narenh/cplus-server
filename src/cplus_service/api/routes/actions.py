"""``GET /actions`` — the tvOS auth checkpoint.

This is the only tvOS-facing route that validates against Seerr for real. It is
called on app launch and whenever the user reconnects to an instance in
settings, and its side effect — writing the Plex-token → user mapping into the
cache — is what makes the cache-only ``/titles/{imdb_id}/actions``, ``/search``
and ``/grab`` possible.

The list returned here is title-agnostic: just the buttons this user could
ever see, by id and name, with no recommendation attached. Getting the
per-title recommendation behind each button is a separate, much more frequent
call — see ``GET /titles/{imdb_id}/actions`` in
:mod:`cplus_service.api.routes.titles` — made on every movie-detail-page view
rather than only at launch.

There is no ``/auth`` route for tvOS and no session token: either this returns
an action list or it 401s, and the client's only recovery is to call it again.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.identity import authenticate_plex_token
from ...db.models import Action, Permission, User
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import DbDep, PlexTokenDep, SeerrDep
from ..schemas import ActionOut, ActionsResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])


async def permitted_actions(session: AsyncSession, user: User) -> list[Action]:
    """Every action this user has been granted, including the Request action."""
    result = await session.execute(
        select(Action)
        .join(Permission, Permission.action_id == Action.id)
        .where(Permission.user_id == user.id)
        .order_by(Action.id)
    )
    return list(result.scalars().all())


@router.get("/actions", response_model=ActionsResponse)
async def get_actions(
    db: DbDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
) -> ActionsResponse:
    """Validate the caller's Plex token and return the actions they may use.

    Permission changes made by the admin land here, and only here — a user
    whose access was revoked keeps it until their next call. That is the
    accepted tradeoff for not doing a live check on every search.
    """
    try:
        user, _auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        # Seerr being unreachable is an upstream fault, not a bad token; saying
        # 401 here would make the client throw away a perfectly good token.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    # The token mapping that backs /search and /grab is refreshed by
    # authenticate_plex_token above, for this and every other live entry point.
    actions = await permitted_actions(db, user)

    return ActionsResponse(
        actions=[ActionOut(id=action.id, name=action.name) for action in actions]
    )
