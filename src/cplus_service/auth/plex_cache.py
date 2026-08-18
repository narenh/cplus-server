"""The Plex-token → local-user cache that backs tvOS auth.

tvOS already holds a user-scoped Plex token, so there is no login step and no
session token for it.  Instead:

* ``GET /actions`` validates the Plex token against Seerr for real, upserts the
  local user, and writes the mapping in here.
* ``/search`` and ``/grab`` read this cache only.  They never call out to Plex
  or Seerr, which is what keeps them fast.
* ``/request`` deliberately bypasses the cache and always validates live,
  because it acts against Seerr as the user.

Deliberately *not* implemented, per the design:

* No TTL or expiry.  An entry is valid until that user's next ``/actions`` call
  overwrites it.
* No persistence.  A restart empties the cache, ``/search`` and ``/grab`` then
  401, and the client re-authenticates on its next ``/actions`` call — which
  tvOS makes on every app launch anyway.
* No revocation push.  Permission changes take effect at the user's next
  ``/actions`` call; that is an accepted tradeoff, not a bug.

Tokens are keyed by SHA-256 rather than stored raw, so a memory dump or a
debugger session does not hand out working Plex credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime


def token_fingerprint(plex_token: str) -> str:
    """Stable, non-reversible cache key for a Plex token."""
    return hashlib.sha256(plex_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedUser:
    """Who a Plex token belongs to, as resolved by the last ``/actions`` call."""

    user_id: int
    seerr_user_id: int
    plex_username: str
    authenticated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PlexTokenCache:
    """In-memory Plex-token → local-user map.

    Safe for concurrent use.  One entry per token; a user who presents a new
    token (Plex reissued it, or they signed in on another device) simply gains a
    second entry, and the stale one is harmless — it maps to the same user.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CachedUser] = {}
        self._lock = asyncio.Lock()

    async def put(self, plex_token: str, user: CachedUser) -> None:
        async with self._lock:
            self._entries[token_fingerprint(plex_token)] = user

    async def get(self, plex_token: str) -> CachedUser | None:
        async with self._lock:
            return self._entries.get(token_fingerprint(plex_token))

    async def forget(self, plex_token: str) -> None:
        async with self._lock:
            self._entries.pop(token_fingerprint(plex_token), None)

    async def forget_user(self, user_id: int) -> None:
        """Drop every token belonging to a user.

        Not used by the request path. The admin UI calls it when removing a
        user, so their cached tokens are invalidated immediately instead of
        leaving them able to search until their next launch.
        """
        async with self._lock:
            stale = [key for key, entry in self._entries.items() if entry.user_id == user_id]
            for key in stale:
                del self._entries[key]

    async def size(self) -> int:
        async with self._lock:
            return len(self._entries)
