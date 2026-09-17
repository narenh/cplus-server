"""Async Radarr API client.

Deliberately thin: this install stores a Radarr URL and key so an admin can
prove the connection works, and nothing more happens with it yet. Radarr's API
is ``v3`` (Prowlarr's is ``v1``), so the two are not one client with a
different base URL, which is why this is its own module rather than a parameter
on :class:`~cplus_service.prowlarr.client.ProwlarrClient`.

The error shape mirrors :class:`~cplus_service.prowlarr.client.ProwlarrError`
on purpose — same two-audience split, so whatever ends up calling this later
handles failures the way the Prowlarr paths already do.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self

import httpx

from .models import SystemStatus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class RadarrError(RuntimeError):
    """Any failure talking to Radarr.

    Wraps transport errors and non-2xx responses alike so callers have one
    thing to catch. ``status_code`` is ``None`` when the request never got a
    response.

    Carries two messages, for two audiences. ``str(exc)`` is the diagnostic
    one — it names the URL and quotes Radarr's own body — and belongs in the
    log and on the admin settings page, where the reader configured that URL
    and can act on what it said. :attr:`summary` is the one an end user gets:
    the same failure with the internal host, the path and Radarr's body left
    out.
    """

    def __init__(
        self,
        message: str,
        *,
        summary: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.summary = summary or "Radarr could not be reached."


class RadarrClient:
    """Thin async wrapper over the Radarr v3 API.

    Usable as an async context manager, or constructed with an externally owned
    ``httpx.AsyncClient`` (the app shares one across its lifespan)::

        async with RadarrClient(base_url, api_key) as radarr:
            status = await radarr.verify_connection()
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if this instance created it."""
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

    # ----------------------------------------------------------------- #
    # Transport
    # ----------------------------------------------------------------- #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        url = f"{self.base_url}/api/v3/{path.lstrip('/')}"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            response = await self.client.request(
                method, url, params=params, json=json, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise RadarrError(
                f"{method} {url} failed: {exc}",
                summary="Could not reach Radarr.",
            ) from exc

        if response.status_code >= 400:
            raise RadarrError(
                f"{method} {url} returned {response.status_code}: {response.text[:500]}",
                summary=f"Radarr returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise RadarrError(
                f"{method} {url} returned a non-JSON body",
                summary="Radarr returned a response this service could not read.",
            ) from exc

    # ----------------------------------------------------------------- #
    # API surface
    # ----------------------------------------------------------------- #

    async def verify_connection(self) -> SystemStatus:
        """Prove the URL and API key are good — backs the admin Verify button.

        Raises :class:`RadarrError` on a bad URL, a bad key (401) or an
        unreachable host.

        Radarr answers ``/system/status`` with a JSON object. A URL that points
        at something else entirely can answer 200 with an array or a bare
        string, which would validate into an empty status and read as success,
        so the shape is checked here rather than trusted.
        """
        payload = await self._request("GET", "system/status")
        if payload is not None and not isinstance(payload, dict):
            raise RadarrError(
                f"GET {self.base_url}/api/v3/system/status returned a JSON "
                f"{type(payload).__name__}, expected an object",
                summary="Radarr returned a response this service could not read.",
            )
        return SystemStatus.model_validate(payload or {})
