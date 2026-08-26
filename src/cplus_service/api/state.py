"""Process-wide resources shared by every request.

Lives apart from the application factory so dependencies can import it without
importing the app, which would be circular (app -> routes -> deps -> app).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@dataclass
class PendingPlexLogin:
    """One in-flight webui sign-in."""

    seerr_url: str
    """The Seerr instance this PIN's resulting token will be validated against."""

    created_at: datetime
    """When the PIN was requested, for :class:`AppState`'s TTL sweep."""


@dataclass
class AppState:
    """Long-lived objects created once in the lifespan and hung off ``app.state``."""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    """Shared outbound client for Prowlarr."""

    seerr_http: httpx.AsyncClient
    """Separate from :attr:`http` on purpose.

    Seerr responses drop a ``connect.sid`` session cookie into whichever
    client's jar sends them. Keeping Seerr on its own client means a user's
    Seerr session can never be attached to an outbound Prowlarr call.
    """

    relay_http: httpx.AsyncClient
    """Third client, for the notification relay.

    Separate from the other two because it is the only one that talks to a
    service outside the admin's own network, and because its timeout is set for
    a different job: a push runs after the response has already gone out, so
    nothing is waiting on it, but a hung connection would still pin a
    background task."""

    pending_plex_logins: dict[int, PendingPlexLogin] = field(default_factory=dict)
    """In-flight webui sign-ins, keyed by plex.tv PIN id.

    ``POST /admin/plex/pin`` takes no auth — it can't, it's the login flow —
    so an abandoned or repeatedly-triggered sign-in must not grow this dict
    without bound. Entries older than
    :data:`~cplus_service.api.routes.admin.login.PENDING_LOGIN_TTL` are swept
    on the next sign-in attempt; see that module."""
