"""Async Prowlarr API client.

Async from the start: search latency is dominated by Prowlarr fanning out to
indexers, and stage 2 will want to issue several searches (one per action's
indexer scope) concurrently.  Nothing calls it concurrently yet.

Every search result handed back has already been through the release parser,
with full discs dropped — callers of this wrapper only ever see clean, tagged,
full-disc-free :class:`~cplus_service.release.models.ParsedRelease` objects.
This is the only place raw Prowlarr release dicts exist.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from ..release.models import ParsedRelease
from ..release.parser import parse_prowlarr_results
from .models import DownloadClient, GrabResult, Indexer, SystemStatus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MOVIE_CATEGORY = 2000


class ProwlarrError(RuntimeError):
    """Any failure talking to Prowlarr.

    Wraps transport errors and non-2xx responses alike so callers have one thing
    to catch.  ``status_code`` is ``None`` when the request never got a response.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProwlarrClient:
    """Thin async wrapper over the Prowlarr v1 API.

    Usable as an async context manager, or constructed with an externally owned
    ``httpx.AsyncClient`` (stage 2 will share one across the app's lifespan)::

        async with ProwlarrClient(base_url, api_key) as prowlarr:
            releases = await prowlarr.search_movie("tt0111161")
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
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            response = await self.client.request(
                method, url, params=params, json=json, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise ProwlarrError(f"{method} {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProwlarrError(
                f"{method} {url} returned {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProwlarrError(f"{method} {url} returned a non-JSON body") from exc

    # ----------------------------------------------------------------- #
    # API surface
    # ----------------------------------------------------------------- #

    async def verify_connection(self) -> SystemStatus:
        """Prove the URL and API key are good — backs the admin Connect/Verify button.

        Raises :class:`ProwlarrError` on a bad URL, a bad key (401) or an
        unreachable host.
        """
        payload = await self._request("GET", "system/status")
        return SystemStatus.model_validate(payload or {})

    async def list_indexers(self) -> list[Indexer]:
        payload = await self._request("GET", "indexer")
        return [Indexer.model_validate(item) for item in payload or []]

    async def list_download_clients(self) -> list[DownloadClient]:
        payload = await self._request("GET", "downloadclient")
        return [DownloadClient.model_validate(item) for item in payload or []]

    async def search_movie(
        self, imdb_id: str, *, indexer_ids: Sequence[int] | None = None
    ) -> list[ParsedRelease]:
        """Search for a movie by IMDB id.

        ``imdb_id`` may be given with or without the ``tt`` prefix.  Search is
        IMDB-driven only — there is deliberately no free-text search anywhere in
        this service, so there is no title-matching ambiguity to resolve.

        ``indexer_ids`` scopes the search to specific indexers; omit it to
        search all of them.  Results come back parsed, tagged and free of full
        discs, in the order Prowlarr returned them — this wrapper does no
        sorting, bucketing or categorisation.
        """
        params: dict[str, Any] = {
            "query": f"{{ImdbId:{_normalize_imdb_id(imdb_id)}}}",
            "type": "movie",
            "categories": [MOVIE_CATEGORY],
        }
        if indexer_ids:
            params["indexerIds"] = list(indexer_ids)

        payload = await self._request("GET", "search", params=params)
        raw_results = payload or []
        releases = parse_prowlarr_results(raw_results)
        logger.debug(
            "prowlarr search imdb=%s raw=%d parsed=%d (full discs dropped=%d)",
            imdb_id,
            len(raw_results),
            len(releases),
            len(raw_results) - len(releases),
        )
        return releases

    async def grab(
        self, *, guid: str, indexer_id: int, download_client_id: int
    ) -> GrabResult:
        """Send a release to a download client.

        Prowlarr's grab endpoint is ``POST /api/v1/search`` with the release
        identity in the body; ``downloadClientId`` selects which of the
        configured clients receives it.
        """
        body = {
            "guid": guid,
            "indexerId": indexer_id,
            "downloadClientId": download_client_id,
        }
        payload = await self._request("POST", "search", json=body)
        raw = payload if isinstance(payload, dict) else {}
        return GrabResult(
            guid=guid,
            indexer_id=indexer_id,
            download_client_id=download_client_id,
            raw=raw,
        )


def _normalize_imdb_id(imdb_id: str) -> str:
    """Prowlarr's ``{ImdbId:...}`` token wants the bare numeric id."""
    value = imdb_id.strip().lower()
    return value[2:] if value.startswith("tt") else value
