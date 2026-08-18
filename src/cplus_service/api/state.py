"""Process-wide resources shared by every request.

Lives apart from the application factory so dependencies can import it without
importing the app, which would be circular (app -> routes -> deps -> app).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


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

    pending_plex_logins: dict[int, str] = field(default_factory=dict)
    """In-flight webui sign-ins: plex.tv PIN id -> the Seerr URL to validate
    against. Held only for the life of a sign-in; abandoning one leaks a single
    short string until restart."""
