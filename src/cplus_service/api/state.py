"""Process-wide resources shared by every request.

Lives apart from the application factory so dependencies can import it without
importing the app, which would be circular (app -> routes -> deps -> app).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..audiobooks.access import PlexAccess
from ..playback.forward import PlaybackLog
from ..tracking.hashes import ReleaseHashCache
from ..tracking.tracker import GrabTracker


@dataclass
class PendingPlexLogin:
    """One in-flight webui sign-in.

    Carries only a timestamp: the Seerr instance to validate against is not a
    property of the sign-in attempt, it is a property of the deployment
    (``CPLUS_SEERR_URL``), so there is nothing here for a caller to have
    chosen. It used to hold the URL, which is precisely how an unauthenticated
    visitor could have had their own server vouch for them.
    """

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

    playback_http: httpx.AsyncClient
    """Fourth client, for forwarding playback events to the admin's webhooks.

    Separate because it posts to arbitrary URLs an admin typed in — a home
    automation hub, usually — and should share no cookie jar with the clients
    that hold a Prowlarr key or a Seerr session. Its own short timeout, too: see
    :data:`~cplus_service.playback.forward.DELIVERY_TIMEOUT_SECONDS`."""

    qbittorrent_http: httpx.AsyncClient
    """Fifth client, for qBittorrent's Web UI (grab tracking).

    Separate because qBittorrent hands out a session cookie that is as good as
    its password, and that cookie has no business in a jar shared with the
    clients that talk to Prowlarr or arbitrary webhook URLs. Short timeouts:
    every call is a small read on the LAN, polled about once a second."""

    pending_plex_logins: dict[int, PendingPlexLogin] = field(default_factory=dict)
    """In-flight webui sign-ins, keyed by plex.tv PIN id.

    ``POST /admin/plex/pin`` takes no auth — it can't, it's the login flow —
    so an abandoned or repeatedly-triggered sign-in must not grow this dict
    without bound. Entries older than
    :data:`~cplus_service.api.routes.admin.login.PENDING_LOGIN_TTL` are swept
    on the next sign-in attempt; see that module."""

    plex_access: PlexAccess = field(default_factory=PlexAccess)
    """Which libraries each Plex user may see, asked of Plex as them and cached
    briefly. See :mod:`cplus_service.audiobooks.access`."""

    playback: PlaybackLog = field(default_factory=PlaybackLog)
    """The Playback tab's recent events and each webhook's last delivery
    result. In memory only, by design: see :mod:`cplus_service.playback`."""

    grab_tracker: GrabTracker = field(default_factory=GrabTracker)
    """Tracked grabs and the tasks tracking them. See :mod:`cplus_service.tracking`."""

    release_hashes: ReleaseHashCache = field(default_factory=ReleaseHashCache)
    """Info hashes seen in recent searches, keyed by release, so a grab can
    find its torrent by hash. See :mod:`cplus_service.tracking.hashes`."""
