"""Turning a Plex token into a local user.

Seerr owns identity. This module is the only place that maps a Seerr user onto
a local ``users`` row, so both auth flows — tvOS's ``/actions`` and the webui's
``/auth`` — agree on what "the same person" means.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Config, User
from ..seerr.client import SeerrClient
from ..seerr.models import SeerrAuth
from .plex_cache import forget_all_tokens, remember_token
from .sessions import destroy_other_sessions

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

    # Every live validation refreshes the stored mapping, not just ``/actions``.
    # The admin app never calls ``/actions`` — that endpoint is tvOS-only — so
    # without this its first ``/seerr/*`` call would leave the mapping empty and
    # ``/search`` and ``/grab`` would 401 for it forever.
    await remember_token(session, plex_token, user)
    return user, auth


async def apply_seerr_url_change(
    session: AsyncSession,
    config: Config,
    new_seerr_url: str | None,
    *,
    keep_session_token: str | None = None,
) -> bool:
    """Point this install at a different Seerr instance, invalidating cached identity.

    Every ``PlexTokenSession`` and browser ``AdminSession`` row was resolved
    against whichever Seerr instance was configured at the time — permissions,
    the ADMIN bit, all of it. Repointing at a different instance without
    dropping those caches would leave every device (and every signed-in
    browser) trusting authorization decisions made by an instance that no
    longer applies, for as long as each cache entry stays unrefreshed. So a
    change here flushes both wholesale rather than tagging rows with which
    instance issued them — the cost is one extra live round trip per device
    (``/actions`` for tvOS, signing in again for the webui), which is already
    the built-in recovery path for a cache miss.

    ``keep_session_token`` lets the caller's own already-verified-this-request
    session survive the flush, so making the change does not immediately sign
    the admin back out of the page they just used to make it.

    A no-op, including no flush, when the URL is unchanged. Returns whether it
    changed, so callers can decide whether to mention the reconnect.
    """
    if config.seerr_url == new_seerr_url:
        return False

    config.seerr_url = new_seerr_url
    await forget_all_tokens(session)
    await destroy_other_sessions(session, keep_token=keep_session_token)
    return True
