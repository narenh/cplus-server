"""Shared request dependencies.

The two auth dependencies here are the enforcement points for the split
described in :mod:`cplus_service.auth.plex_cache`:

``cached_user``
    Cache-only. Used by ``/search`` and ``/grab``, which must never make an
    outbound call to Plex or Seerr.

``live_user``
    Always validates against Seerr. Used by ``/actions`` (which is the
    checkpoint that populates the cache) and by ``/request`` (which needs a
    Seerr session to act as the user).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.plex_cache import CachedUser
from ..auth.sessions import SESSION_COOKIE_NAME, resolve_session
from ..db.models import Config, User
from ..db.session import get_config
from ..prowlarr.client import ProwlarrClient
from ..seerr.client import SeerrClient
from .state import AppState

PLEX_TOKEN_HEADER = "X-Plex-Token"


def get_state(request: Request) -> AppState:
    return request.app.state.cplus


StateDep = Annotated[AppState, Depends(get_state)]


async def get_db(state: StateDep) -> AsyncIterator[AsyncSession]:
    """A transactional session per request: commits on success, rolls back on error."""
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


async def get_seerr(state: StateDep, config: ConfigDep) -> SeerrClient:
    if not config.seerr_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Seerr is not configured yet. Set it in the admin settings.",
        )
    return SeerrClient(config.seerr_url, client=state.seerr_http)


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


async def get_cached_user(state: StateDep, plex_token: PlexTokenDep) -> CachedUser:
    """Resolve the caller from the Plex-token cache alone.

    A miss is a 401 with a body telling the client what to do about it: call
    ``/actions`` again. That happens on the next app launch regardless, so this
    path is mostly hit when the server restarted mid-session.
    """
    cached = await state.plex_cache.get(plex_token)
    if cached is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Unrecognised Plex token. Call GET /actions to authenticate first.",
        )
    return cached


CachedUserDep = Annotated[CachedUser, Depends(get_cached_user)]


async def get_admin(
    db: DbDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> User:
    """Resolve the admin behind a webui session cookie.

    Stage 3's admin routes depend on this; the stubs do not, so that the route
    structure is browsable before the webui exists.
    """
    user = await resolve_session(db, session_cookie)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


AdminDep = Annotated[User, Depends(get_admin)]
