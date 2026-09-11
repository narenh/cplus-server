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
from collections.abc import Mapping, Sequence
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

    Carries two messages, for two audiences.  ``str(exc)`` is the diagnostic
    one — it names the URL and quotes Prowlarr's own body — and belongs in the
    log and on the admin settings page, where the reader configured that URL and
    can act on what it said.  :attr:`summary` is the one an end user gets: the
    same failure with the internal host, the path and Prowlarr's body left out.
    Neither the app nor its user has any business seeing where this service
    keeps Prowlarr, and none of that detail is theirs to act on.
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
        self.summary = summary or "Prowlarr could not be reached."


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
            raise ProwlarrError(
                f"{method} {url} failed: {exc}",
                summary="Could not reach Prowlarr.",
            ) from exc

        if response.status_code >= 400:
            raise ProwlarrError(
                f"{method} {url} returned {response.status_code}: {response.text[:500]}",
                summary=f"Prowlarr returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProwlarrError(
                f"{method} {url} returned a non-JSON body",
                summary="Prowlarr returned a response this service could not read.",
            ) from exc

    async def _request_list(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> list[Any]:
        """:meth:`_request` for the endpoints whose answer is a JSON array.

        Prowlarr answers these with an array, but not unconditionally: an
        indexer failure, a misrouted request or a proxy sitting in front of it
        can put an object — or a bare string — there instead, still under a
        200.  Iterating that yields something the callers below are not written
        for (a dict iterates as its *keys*), and they fail somewhere further
        down with an ``AttributeError`` nobody catches, which is how a search
        that simply found nothing turned into a 500.

        Rejecting the shape here instead puts every such case on the one path
        callers already handle, and a genuinely empty result — ``[]``, ``null``
        or an empty body — stays the empty list it should be.
        """
        payload = await self._request(method, path, params=params, json=json)
        if payload is None:
            return []
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        if not isinstance(payload, list):
            raise ProwlarrError(
                f"{method} {url} returned a JSON {type(payload).__name__}, "
                f"expected an array",
                summary="Prowlarr returned a response this service could not read.",
            )
        if not all(isinstance(item, Mapping) for item in payload):
            raise ProwlarrError(
                f"{method} {url} returned an array holding something other than objects",
                summary="Prowlarr returned a response this service could not read.",
            )
        return payload

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
        payload = await self._request_list("GET", "indexer")
        return [Indexer.model_validate(item) for item in payload]

    async def list_download_clients(self) -> list[DownloadClient]:
        payload = await self._request_list("GET", "downloadclient")
        return [DownloadClient.model_validate(item) for item in payload]

    async def search_movie(
        self, imdb_id: str, *, indexer_ids: Sequence[int] | None = None
    ) -> list[ParsedRelease]:
        """Search for a movie by IMDB id.

        ``imdb_id`` may be given with or without the ``tt`` prefix.  Being
        IMDB-keyed, this path has no title-matching ambiguity to resolve, and is
        scoped to the movie category.  See :meth:`search_query` for the
        free-text path, which is neither.

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

        raw_results = await self._request_list("GET", "search", params=params)
        releases = parse_prowlarr_results(raw_results)
        logger.debug(
            "prowlarr search imdb=%s raw=%d parsed=%d (full discs dropped=%d)",
            imdb_id,
            len(raw_results),
            len(releases),
            len(raw_results) - len(releases),
        )
        return releases

    async def search_query(
        self, query: str, *, indexer_ids: Sequence[int] | None = None
    ) -> list[ParsedRelease]:
        """Search for an arbitrary string.

        Unlike :meth:`search_movie` this is **not** category-scoped: the user
        typed the string, so TV, anime and everything else are fair game.  It is
        also inherently ambiguous — Prowlarr matches on the text, and nothing
        here tries to resolve what the user meant.

        Results are parsed and full-disc-filtered exactly as for a movie search,
        so callers get the same tagged shape.  Be aware the parser is tuned for
        movie release names: a TV release will still tag its resolution, source
        and HDR correctly, while ``base_title`` and ``release_group`` are less
        meaningful.
        """
        params: dict[str, Any] = {"query": query, "type": "search"}
        if indexer_ids:
            params["indexerIds"] = list(indexer_ids)

        raw_results = await self._request_list("GET", "search", params=params)
        releases = parse_prowlarr_results(raw_results)
        logger.debug(
            "prowlarr text search %r raw=%d parsed=%d (full discs dropped=%d)",
            query,
            len(raw_results),
            len(releases),
            len(raw_results) - len(releases),
        )
        return releases

    async def grab(
        self, *, guid: str, indexer_id: int, download_client_id: int | None = None
    ) -> GrabResult:
        """Send a release to a download client.

        Prowlarr's grab endpoint is ``POST /api/v1/search`` with the release
        identity in the body; ``downloadClientId`` selects which of the
        configured clients receives it.

        Omitted entirely when ``download_client_id`` is ``None``, which is how
        Prowlarr is asked to use its own default client for the release's
        protocol. Sending an explicit ``null`` is not the same thing — Prowlarr
        reads the key as present and rejects it — so the field is left out of
        the body rather than set to ``None``.
        """
        body: dict[str, Any] = {
            "guid": guid,
            "indexerId": indexer_id,
        }
        if download_client_id is not None:
            body["downloadClientId"] = download_client_id
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
