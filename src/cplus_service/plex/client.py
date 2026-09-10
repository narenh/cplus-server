"""Talking to plex.tv, and — for the Libraries & Home tab — to the admin's own
Plex Media Server.

:class:`PlexPinClient` is the OAuth PIN flow: the admin webui has no Plex token
of its own to start with, so it asks plex.tv for a short code, sends the
browser to Plex to claim it, then polls until Plex hands back an auth token. We
proxy both halves rather than doing it from browser JavaScript, which keeps the
flow working regardless of plex.tv's CORS policy, keeps the resulting Plex
token out of page scripts entirely, and leaves the browser with nothing to do
but poll one URL.

:func:`discover_resources` and :class:`PlexServerClient` are what the token from
that flow is kept for afterwards (see ``Config.plex_admin_token``): plex.tv is
the only place that knows which server currently answers to the admin's account
and how to reach it, and the server itself is the only place that knows what
libraries it holds — the same two calls CanopyPlus itself makes from
``PlexServer``.

Everything here talks either to plex.tv or to the admin's own server, never to
another user's. tvOS arrives with its own token already in hand and never
routes through this module; user identity there and everywhere else is always
resolved through Seerr.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

PLEX_API = "https://plex.tv/api/v2"
PLEX_AUTH_APP = "https://app.plex.tv/auth"

PRODUCT_NAME = "cplus-service"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PlexError(RuntimeError):
    """Any failure talking to plex.tv."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PlexPinClient:
    """Minimal plex.tv PIN-flow client.

    ``client_identifier`` must be stable across the two halves of a single
    flow — plex.tv ties the PIN to it — and stable across sign-ins, or each
    login registers a new device on the admin's Plex account. It is persisted
    per install in ``config.plex_client_identifier``.
    """

    def __init__(
        self,
        client_identifier: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.client_identifier = client_identifier
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT_NAME,
            "X-Plex-Client-Identifier": self.client_identifier,
        }

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, url, headers=self._headers(), timeout=self._timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"{method} {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise PlexError(
                f"plex.tv returned {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlexError("plex.tv returned a non-JSON body") from exc
        return payload if isinstance(payload, dict) else {}

    async def create_pin(self) -> tuple[int, str]:
        """Ask plex.tv for a new PIN. Returns ``(pin_id, code)``."""
        payload = await self._request("POST", f"{PLEX_API}/pins", params={"strong": "true"})
        pin_id, code = payload.get("id"), payload.get("code")
        if not pin_id or not code:
            raise PlexError("plex.tv did not return a usable PIN")
        return int(pin_id), str(code)

    def auth_url(self, code: str, *, forward_url: str | None = None) -> str:
        """The plex.tv page the admin's browser must visit to claim a PIN."""
        params = {
            "clientID": self.client_identifier,
            "code": code,
            "context[device][product]": PRODUCT_NAME,
        }
        if forward_url:
            params["forwardUrl"] = forward_url
        return f"{PLEX_AUTH_APP}#?{urlencode(params)}"

    async def check_pin(self, pin_id: int) -> str | None:
        """Poll a PIN. Returns the Plex auth token once claimed, else ``None``.

        An unclaimed PIN is a normal, expected outcome — the admin simply has
        not finished signing in yet — so it is not an error. plex.tv answers
        404 once a PIN has expired, which surfaces as :class:`PlexError`.
        """
        payload = await self._request("GET", f"{PLEX_API}/pins/{pin_id}")
        token = payload.get("authToken")
        return str(token) if token else None


# --------------------------------------------------------------------------- #
# Server discovery and library sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlexConnection:
    """One way to reach a Plex resource, as plex.tv's resources list gives it."""

    protocol: str
    address: str
    port: int
    uri: str
    local: bool
    relay: bool


@dataclass(frozen=True)
class PlexResource:
    """One entry from plex.tv's ``/resources`` — a server, a player, or a client.

    Only servers matter here (:attr:`is_server`); the rest are listed too but
    the caller filters them out.
    """

    name: str
    client_identifier: str
    provides: str
    owned: bool
    access_token: str | None
    connections: tuple[PlexConnection, ...] = field(default_factory=tuple)

    @property
    def is_server(self) -> bool:
        return "server" in (part.strip() for part in self.provides.split(","))


@dataclass(frozen=True)
class PlexLibrarySection:
    """One Plex library section, straight off ``GET /library/sections``.

    Same fields CanopyPlus's own ``PlexLibrary`` decodes — ``key`` as ``id``,
    ``title`` as ``name`` — so the admin webui and the app agree on what a
    library is.
    """

    id: str
    name: str
    type: str
    hidden: bool


@dataclass(frozen=True)
class PlexCollection:
    """One collection within a library, straight off ``GET /library/sections/{id}/collections``.

    ``id`` is Plex's ``ratingKey`` — unique server-wide, not scoped to the
    library it was fetched from, but the caller always already knows which
    library it asked about.
    """

    id: str
    title: str


def best_connection(connections: Sequence[PlexConnection]) -> PlexConnection | None:
    """Prefer a local, direct connection; fall back to remote, then relay.

    A relay connection works but is slower and routes through Plex's own
    infrastructure, so it is the last resort rather than whichever plex.tv
    happened to list first. ``None`` only when there is nothing to try at all.
    """
    if not connections:
        return None
    return min(connections, key=lambda c: (c.relay, not c.local))


async def discover_resources(
    token: str,
    client_identifier: str,
    *,
    client: httpx.AsyncClient,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> list[PlexResource]:
    """The Plex resources (servers included) reachable with ``token``.

    Called once after a successful admin sign-in (see
    :func:`cplus_service.auth.identity.refresh_plex_server`) and again from the
    Libraries page's "Reconnect" button — plex.tv is the only place that knows
    which machine currently answers to this account and how to reach it, so
    there is no way to ask the server itself without asking plex.tv first.
    """
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT_NAME,
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Token": token,
    }
    url = f"{PLEX_API}/resources"
    try:
        response = await client.get(
            url,
            headers=headers,
            params={"includeHttps": "1", "includeRelay": "1"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise PlexError(f"GET {url} failed: {exc}") from exc

    if response.status_code >= 400:
        raise PlexError(
            f"plex.tv returned {response.status_code}", status_code=response.status_code
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise PlexError("plex.tv returned a non-JSON body") from exc

    if not isinstance(payload, list):
        return []

    resources: list[PlexResource] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        connections = tuple(
            PlexConnection(
                protocol=str(conn.get("protocol", "")),
                address=str(conn.get("address", "")),
                port=int(conn.get("port") or 0),
                uri=str(conn.get("uri", "")),
                local=bool(conn.get("local", False)),
                relay=bool(conn.get("relay", False)),
            )
            for conn in raw.get("connections", [])
            if isinstance(conn, dict)
        )
        resources.append(
            PlexResource(
                name=str(raw.get("name", "")),
                client_identifier=str(raw.get("clientIdentifier", "")),
                provides=str(raw.get("provides", "")),
                owned=bool(raw.get("owned", False)),
                access_token=raw.get("accessToken") or None,
                connections=connections,
            )
        )
    return resources


class PlexServerError(PlexError):
    """Talking to the Plex Media Server itself (not plex.tv) failed."""


class PlexServerClient:
    """Direct client for the admin's own Plex Media Server.

    Unlike :class:`PlexPinClient`, which only ever talks to plex.tv, this talks
    straight to the server — the same request CanopyPlus itself makes from
    ``PlexServer.fetchLibraries()`` — so the Default Libraries picker shows
    exactly what is on the server, not a stale copy.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def list_library_sections(self) -> list[PlexLibrarySection]:
        """``GET /library/sections`` — every library the server holds.

        Includes every type Plex has (movie, show, artist, photo, video); it is
        up to the caller to filter to what Canopy+ can actually show.
        """
        url = f"{self.base_url}/library/sections"
        headers = {"Accept": "application/json", "X-Plex-Token": self.token}
        try:
            response = await self.client.get(url, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise PlexServerError(f"GET {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise PlexServerError(
                f"Plex server returned {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlexServerError("Plex server returned a non-JSON body") from exc

        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        directory = container.get("Directory", []) if isinstance(container, dict) else []

        sections: list[PlexLibrarySection] = []
        for raw in directory:
            if not isinstance(raw, dict):
                continue
            key, title, kind = raw.get("key"), raw.get("title"), raw.get("type")
            if not key or not title or not kind:
                continue
            sections.append(
                PlexLibrarySection(
                    id=str(key),
                    name=str(title),
                    type=str(kind),
                    hidden=bool(int(raw.get("hidden", 0) or 0)),
                )
            )
        return sections

    async def list_collections(self, library_id: str) -> list[PlexCollection]:
        """``GET /library/sections/{library_id}/collections`` — that library's collections.

        Backs the Home shelf editor's "Collection Items…" picker — the same
        request CanopyPlus's own ``PlexServer.items(path: .libraryItems(_,
        .collections))`` makes.
        """
        url = f"{self.base_url}/library/sections/{library_id}/collections"
        headers = {"Accept": "application/json", "X-Plex-Token": self.token}
        try:
            response = await self.client.get(url, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise PlexServerError(f"GET {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise PlexServerError(
                f"Plex server returned {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlexServerError("Plex server returned a non-JSON body") from exc

        container = payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        items = container.get("Metadata", []) if isinstance(container, dict) else []

        collections: list[PlexCollection] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            rating_key, title = raw.get("ratingKey"), raw.get("title")
            if not rating_key or not title:
                continue
            collections.append(PlexCollection(id=str(rating_key), title=str(title)))
        return collections
