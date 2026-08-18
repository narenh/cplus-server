"""Plex OAuth PIN flow, proxied server-side.

The admin webui has no Plex token of its own, so it runs the standard PIN flow:
ask plex.tv for a short code, send the browser to Plex to claim it, then poll
until Plex hands back an auth token.

We proxy both halves rather than doing it from browser JavaScript. That keeps
the flow working regardless of plex.tv's CORS policy, keeps the resulting Plex
token out of page scripts entirely, and leaves the browser with nothing to do
but poll one URL.

This is the **only** place cplus-service talks to plex.tv, and it is only ever
used by the webui sign-in. tvOS arrives with a token already in hand, and user
identity is always resolved through Seerr, never here.
"""

from __future__ import annotations

import logging
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
