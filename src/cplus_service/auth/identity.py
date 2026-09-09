"""Turning a Plex token into a local user.

Seerr owns identity. This module is the only place that maps a Seerr user onto
a local ``users`` row, so both auth flows — tvOS's ``/register`` and the webui's
``/auth`` — agree on what "the same person" means.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Config, User
from ..plex.client import PlexError, best_connection, discover_resources
from ..seerr.client import SeerrClient
from ..seerr.models import SeerrAuth
from ..settings import seerr_url_fingerprint
from .plex_cache import forget_all_tokens, remember_token
from .sessions import destroy_all_sessions

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

    # Every live validation refreshes the stored mapping, not just ``/register``.
    # The admin app never calls ``/register`` — that endpoint is tvOS-only — so
    # without this its first ``/seerr/*`` call would leave the mapping empty and
    # ``/search`` and ``/grab`` would 401 for it forever.
    await remember_token(session, plex_token, user)
    return user, auth


async def sync_seerr_instance(session: AsyncSession, config: Config) -> bool:
    """Flush every cached credential if the deployment was repointed at another Seerr.

    Called once per startup, before the app serves anything. Every
    ``PlexTokenSession`` and browser ``AdminSession`` row was resolved against
    whichever Seerr instance was configured when it was written — permissions,
    the ADMIN bit, all of it. Coming up against a different instance without
    dropping those caches would leave every device, and every signed-in
    browser, trusting authorization decisions made by an instance that no
    longer applies. So a change flushes both wholesale rather than tagging rows
    with which instance issued them — the cost is one extra live round trip per
    device (``/register`` for tvOS, signing in again for the webui), which is
    already the built-in recovery path for a cache miss.

    Startup is the only place this can happen now that the URL comes from the
    environment: it cannot change while the process is running, so there is no
    request to flush from and no session of the admin's own to spare. That also
    makes the first boot after this change a flush — the fingerprint starts
    empty — which is the correct reading of "we cannot prove what these were
    resolved against".

    Returns whether it flushed, for the startup log line.
    """
    fingerprint = seerr_url_fingerprint()
    if config.seerr_url_fingerprint == fingerprint:
        return False

    config.seerr_url_fingerprint = fingerprint
    await forget_all_tokens(session)
    await destroy_all_sessions(session)
    return True


async def refresh_plex_server(
    config: Config, token: str, client_identifier: str, http: httpx.AsyncClient
) -> bool:
    """Rediscover the admin's Plex Media Server and remember how to reach it.

    Called once after every successful admin sign-in, and again on demand from
    the Libraries & Home page's "Reconnect" button. Best-effort and silent on
    failure: Prowlarr, Seerr and every other tab stay fully usable without a
    Plex server connection, so a plex.tv hiccup here must never fail the sign-in
    it rides along with — it only leaves the Default Libraries picker showing
    "not connected" until the next attempt.

    Picks the account's own server when there is one (falling back to the
    first server resource plex.tv lists, for a Home-user admin signing in on a
    server they do not own), and the best reachable connection to it. Returns
    whether a server was found and stored.
    """
    try:
        resources = await discover_resources(token, client_identifier, client=http)
    except PlexError as exc:
        logger.warning("could not discover the Plex server: %s", exc)
        return False

    servers = [resource for resource in resources if resource.is_server]
    if not servers:
        logger.warning("signed-in Plex account has no server plex.tv will list")
        return False
    server = next((s for s in servers if s.owned), servers[0])

    connection = best_connection(server.connections)
    if connection is None:
        logger.warning("Plex server %r has no usable connection", server.name)
        return False

    config.plex_admin_token = server.access_token or token
    config.plex_server_base_url = connection.uri
    config.plex_server_client_identifier = server.client_identifier
    config.plex_server_name = server.name
    logger.info("connected to Plex server %r via %s", server.name, connection.uri)
    return True
