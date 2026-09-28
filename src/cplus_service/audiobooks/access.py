"""Which audiobooks a Plex user may read: asked of Plex, as them.

An alignment holds a book's entire text, so it goes only to people who can
already see the book in Plex. This service does not keep users' Plex tokens (it
stores fingerprints), but every client request carries one. With it, plex.tv
gives that user's own access token for this install's server, and the server,
asked with that token, lists the libraries it shares with them — the same
answer Plex's own apps get.

Both answers are cached in memory, per token, for :data:`TTL` seconds, so a
reading session costs Plex one or two calls rather than one per page. A newly
shared (or unshared) library is noticed within that time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..auth.plex_cache import token_fingerprint
from ..db.models import Config
from ..plex.client import PlexError, PlexServerClient, discover_resources

TTL = 600.0


class AccessUnknown(RuntimeError):
    """Plex couldn't be asked, so nobody can say what this user may see."""


@dataclass
class PlexAccess:
    _cache: dict[tuple[str, str], tuple[float, Any]] = field(default_factory=dict)

    def _get(self, key: tuple[str, str]) -> tuple[bool, Any]:
        hit = self._cache.get(key)
        if hit is None or hit[0] < time.monotonic():
            return False, None
        return True, hit[1]

    def _put(self, key: tuple[str, str], value: Any) -> Any:
        if len(self._cache) > 5000:  # never grows without bound
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v[0] >= now}
        self._cache[key] = (time.monotonic() + TTL, value)
        return value

    async def server_token(
        self, user_token: str, config: Config, http: httpx.AsyncClient
    ) -> str | None:
        """This user's token for this install's Plex server; ``None`` if not shared with them."""
        key = ("server", token_fingerprint(user_token))
        found, value = self._get(key)
        if found:
            return value
        try:
            resources = await discover_resources(
                user_token, config.plex_client_identifier or "cplus-service", client=http
            )
        except PlexError as exc:
            if exc.status_code in (401, 403):
                return self._put(key, None)
            raise AccessUnknown(f"plex.tv: {exc}") from exc
        match = next(
            (r for r in resources if r.client_identifier == config.plex_server_client_identifier),
            None,
        )
        return self._put(key, match.access_token if match else None)

    def _server(self, config: Config, token: str, http: httpx.AsyncClient) -> PlexServerClient:
        return PlexServerClient(config.plex_server_base_url or "", token, client=http)

    async def libraries(
        self, user_token: str, config: Config, http: httpx.AsyncClient
    ) -> set[str]:
        """Ids of the libraries on this server the user can see."""
        key = ("libraries", token_fingerprint(user_token))
        found, value = self._get(key)
        if found:
            return value
        token = await self.server_token(user_token, config, http)
        if token is None or not config.plex_server_base_url:
            return self._put(key, set())
        try:
            sections = await self._server(config, token, http).list_library_sections()
        except PlexError as exc:
            if exc.status_code in (401, 403):
                return self._put(key, set())
            raise AccessUnknown(f"Plex server: {exc}") from exc
        return self._put(key, {section.id for section in sections})

    async def can_see_album(
        self, user_token: str, config: Config, http: httpx.AsyncClient, rating_key: str
    ) -> bool:
        """Whether this album is visible to the user, for a book with no alignment to go by."""
        key = (f"album:{rating_key}", token_fingerprint(user_token))
        found, value = self._get(key)
        if found:
            return value
        token = await self.server_token(user_token, config, http)
        if token is None or not config.plex_server_base_url:
            return self._put(key, False)
        try:
            album = await self._server(config, token, http).album(rating_key, token=token)
        except PlexError as exc:
            raise AccessUnknown(f"Plex server: {exc}") from exc
        return self._put(key, album is not None)
