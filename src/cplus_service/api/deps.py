"""Shared request dependencies.

``get_cached_user`` is the enforcement point for the split described in
:mod:`cplus_service.auth.plex_cache`: it resolves a caller from the stored
Plex-token mapping without any outbound call, and backs
``/titles/{imdb_id}/actions``, ``/search`` and ``/grab``. ``/register`` and
``/request`` validate against Seerr directly instead, so they do not use it.

Which Seerr they validate against is never a request's to choose:
:func:`get_seerr` reads it from the environment, so no caller — authenticated or
not — can name the instance that decides whether they are an admin.

Admin routes are gated by
:func:`cplus_service.api.routes.admin.deps.require_admin_page`, which redirects
a signed-out browser rather than answering 401 JSON.

:func:`get_calling_device` is the odd one out: it authorises nothing. It reads
the caller's ``X-Plex-Client-Identifier`` so an event can record *which screen*
it happened on as well as who, and it is deliberately optional at every step —
see its docstring.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.plex_cache import resolve_token
from ..db.models import Config, PlexDevice, User
from ..db.session import get_config
from ..prowlarr.client import ProwlarrClient
from ..seerr.client import SeerrClient
from ..seerr.models import SeerrAuth
from ..settings import SEERR_URL_ENV, seerr_url
from .state import AppState

logger = logging.getLogger(__name__)

PLEX_TOKEN_HEADER = "X-Plex-Token"

#: Plex's own header names, not ones invented here. Every Plex client already
#: computes both for plex.tv itself — CanopyPlus keeps its identifier in
#: ``UserDefaults`` under ``PlexClientIdKey`` — so a client sending them here is
#: forwarding what it holds rather than deriving anything new.
PLEX_CLIENT_IDENTIFIER_HEADER = "X-Plex-Client-Identifier"
PLEX_DEVICE_NAME_HEADER = "X-Plex-Device-Name"

#: Column widths in :class:`~cplus_service.db.models.PlexDevice`. A header over
#: the limit is dropped rather than truncated: a truncated identifier is a
#: *different* device as far as every lookup is concerned, and silently
#: merging two of them would be worse than logging the event with no device at
#: all.
MAX_CLIENT_IDENTIFIER_LENGTH = 128
MAX_DEVICE_NAME_LENGTH = 256


def get_state(request: Request) -> AppState:
    return request.app.state.cplus


StateDep = Annotated[AppState, Depends(get_state)]


async def get_db(state: StateDep) -> AsyncIterator[AsyncSession]:
    """A transactional session per request: commits on success, rolls back on error.

    **A handler returning a ``StreamingResponse`` must commit before it
    returns.** This dependency's exit code runs only once the response body has
    finished streaming, so everything the handler wrote stays in an open
    transaction for as long as the stream lasts — minutes, for a search waiting
    on Prowlarr. SQLite holds a write lock for that whole time and every other
    request that writes (which is all of them: authentication refreshes the
    caller's token mapping) waits on it and then fails ``database is locked``.
    Committing at the end of the handler ends the transaction before the first
    byte of the body is produced; the commit here is then a no-op.
    """
    async with state.sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbDep = Annotated[AsyncSession, Depends(get_db)]


async def get_settings(db: DbDep) -> Config:
    return await get_config(db)


ConfigDep = Annotated[Config, Depends(get_settings)]


async def get_prowlarr(state: StateDep, config: ConfigDep) -> ProwlarrClient:
    if not config.prowlarr_url or not config.prowlarr_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Prowlarr is not configured yet. Set it in the admin settings.",
        )
    return ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key, client=state.http)


ProwlarrDep = Annotated[ProwlarrClient, Depends(get_prowlarr)]


async def get_seerr(state: StateDep) -> SeerrClient:
    """A client for the Seerr instance named by the environment.

    Deliberately does not take :data:`ConfigDep`: the URL is not in the database
    and cannot be set through any request. Seerr decides who is admin, so a
    request-supplied URL would be a request-supplied answer to "am I admin?".
    See :mod:`cplus_service.settings`.
    """
    url = seerr_url()
    if not url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Seerr is not configured. Set {SEERR_URL_ENV} and restart.",
        )
    return SeerrClient(url, client=state.seerr_http)


SeerrDep = Annotated[SeerrClient, Depends(get_seerr)]


async def get_plex_token(
    x_plex_token: Annotated[str | None, Header(alias=PLEX_TOKEN_HEADER)] = None,
) -> str:
    if not x_plex_token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"Missing {PLEX_TOKEN_HEADER} header"
        )
    return x_plex_token


PlexTokenDep = Annotated[str, Depends(get_plex_token)]


async def get_cached_user(db: DbDep, plex_token: PlexTokenDep) -> User:
    """Resolve the caller from the stored Plex-token mapping alone.

    No outbound call to Plex or Seerr — that is the whole point. A miss means
    this token has never been through ``/register``, so the 401 says exactly
    that. The mapping is persisted, so a restart no longer causes one.
    """
    user = await resolve_token(db, plex_token)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Unrecognised Plex token. Call GET /register to authenticate first.",
        )
    return user


CachedUserDep = Annotated[User, Depends(get_cached_user)]


def _clean_header(value: str | None, *, limit: int) -> str | None:
    """A client-supplied label, or ``None`` if there is nothing usable in it.

    Control characters are stripped rather than escaped: these values end up in
    server logs, where a raw newline can forge a log line, and the admin console
    renders them (escaped by Jinja) next to values an admin has to be able to
    read. Nothing downstream needs them.
    """
    if value is None:
        return None
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


async def get_calling_device(
    db: DbDep,
    x_plex_client_identifier: Annotated[
        str | None, Header(alias=PLEX_CLIENT_IDENTIFIER_HEADER)
    ] = None,
    x_plex_device_name: Annotated[str | None, Header(alias=PLEX_DEVICE_NAME_HEADER)] = None,
) -> str | None:
    """Which install is calling, recording it in the device registry on the way.

    Returns the identifier alone, because that is all a handler ever needs to
    write onto its ``grabs`` or ``activity_log`` row. Keeping the registry
    current — creating the row on first sighting, refreshing ``last_seen_at``
    and the self-reported name after that — is this dependency's side effect,
    so no handler has to remember to do it and the two can never disagree about
    which devices exist.

    **Never raises.** A missing header is the ordinary state of every client
    that predates it, and one this service has to keep serving unchanged; a
    malformed one is a client bug that must not cost a user their grab. Both
    answer ``None``, which every caller stores as "device unknown".

    Runs inside the request's own transaction (``DbDep``), so the sighting is
    committed by whatever the handler does — including the streaming handlers,
    which commit before their first byte and therefore carry this with them.
    """
    identifier = _clean_header(
        x_plex_client_identifier, limit=MAX_CLIENT_IDENTIFIER_LENGTH
    )
    if identifier is None:
        if x_plex_client_identifier:
            logger.debug("ignoring an unusable %s header", PLEX_CLIENT_IDENTIFIER_HEADER)
        return None

    device_name = _clean_header(x_plex_device_name, limit=MAX_DEVICE_NAME_LENGTH)

    device = await db.get(PlexDevice, identifier)
    if device is None:
        device = PlexDevice(client_identifier=identifier, device_name=device_name)
        db.add(device)
    else:
        # Only when the client actually said something. A client that stops
        # sending the name keeps whatever it last reported, rather than having
        # the row fall back to a bare UUID in the admin console.
        if device_name is not None:
            device.device_name = device_name
        device.last_seen_at = datetime.now(UTC)

    return identifier


#: The calling install's Plex client identifier, or ``None`` if it did not say.
DeviceDep = Annotated[str | None, Depends(get_calling_device)]



def require_request_manager(auth: SeerrAuth) -> None:
    """Refuse a caller who may not manage requests.

    The gate for the admin app's operations: approving or declining a request,
    and grabbing a release directly without going through an action. Mirrors
    Seerr's own guard on those endpoints, ``MANAGE_REQUESTS``, with the owner
    passing implicitly because Seerr treats ADMIN as implying everything.

    Checked here rather than left to Seerr's 403 so the rule is stated in our
    code — and because a direct grab never reaches Seerr at all.
    """
    if not auth.user.can_manage_requests:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This action is limited to users who can manage requests.",
        )
