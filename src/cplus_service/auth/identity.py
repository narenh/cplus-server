"""Turning a Plex token into a local user.

Seerr owns identity. This module is the only place that maps a Seerr user onto
a local ``users`` row, so both auth flows — tvOS's ``/actions`` and the webui's
``/auth`` — agree on what "the same person" means.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from ..seerr.client import SeerrClient
from ..seerr.models import SeerrAuth

logger = logging.getLogger(__name__)


async def upsert_user(session: AsyncSession, auth: SeerrAuth) -> User:
    """Find or create the local row for a Seerr user.

    ``seerr_user_id`` is the join key. ``plex_username`` is a cache for the
    admin UI and is refreshed on every login, so a Plex rename shows up without
    any explicit sync.
    """
    result = await session.execute(
        select(User).where(User.seerr_user_id == auth.user.id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(seerr_user_id=auth.user.id, plex_username=auth.user.best_username)
        session.add(user)
        await session.flush()
        logger.info("registered new user seerr_user_id=%s", auth.user.id)
    elif user.plex_username != auth.user.best_username:
        user.plex_username = auth.user.best_username

    return user


async def authenticate_plex_token(
    session: AsyncSession, seerr: SeerrClient, plex_token: str
) -> tuple[User, SeerrAuth]:
    """Validate a Plex token against Seerr and resolve it to a local user.

    Raises :class:`~cplus_service.seerr.client.SeerrAuthError` if Seerr rejects
    the token, and :class:`~cplus_service.seerr.client.SeerrError` if Seerr
    could not be reached — callers must keep those distinct, since an
    unreachable Seerr is a 502, not a 401.
    """
    auth = await seerr.authenticate_plex(plex_token)
    user = await upsert_user(session, auth)
    return user, auth
