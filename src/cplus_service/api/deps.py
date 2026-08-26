"""Shared request dependencies.

``get_cached_user`` is the enforcement point for the split described in
:mod:`cplus_service.auth.plex_cache`: it resolves a caller from the stored
Plex-token mapping without any outbound call, and backs
``/titles/{imdb_id}/actions``, ``/search`` and ``/grab``. ``/register`` and
``/request`` validate against Seerr directly instead, so they do not use it.

Admin routes are gated by
:func:`cplus_service.api.routes.admin.deps.require_admin_page`, which redirects
a signed-out browser rather than answering 401 JSON.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.plex_cache import resolve_token
from ..db.models import Config, User
from ..db.session import get_config
from ..prowlarr.client import ProwlarrClient
from ..seerr.client import SeerrClient
from ..seerr.models import SeerrAuth
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


def require_admin(auth: SeerrAuth) -> None:
    """Refuse a caller who is not a Seerr admin.

    Stricter than :func:`require_request_manager` — ``MANAGE_REQUESTS`` does
    not imply ``ADMIN``, only the reverse. Checked here rather than left to
    Seerr, which has no opinion on this endpoint at all: it is config this
    service owns, not a passthrough call.
    """
    if not auth.user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This action is limited to admins.",
        )
