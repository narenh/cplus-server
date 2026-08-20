"""Async Seerr API client.

``authenticate_plex`` trades a Plex auth token for a Seerr user identity, and is
the single source of truth for "who is this?" in the whole service — cplus never
talks to plex.tv itself outside the admin sign-in, and never stores a password.

Every other call here is made **as that user**, with the session cookie
``authenticate_plex`` opened: creating a request, listing requests, approving or
declining one, deleting one, and reading ``/auth/me``.  Nothing is done with an
API key, because cplus holds no Seerr credential at all — only a URL.  That is
what lets these back client features whose credential would otherwise have to
live on every device.

The set of operations is a deliberate allowlist, not a general proxy.  Seerr's
``/settings/*`` endpoints return the Radarr, Sonarr and Overseerr API keys to
any caller with owner authority, so a blanket proxy would hand them straight
back out — the same failure this service exists to prevent for Prowlarr.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from .models import SeerrAuth, SeerrRequestResult, SeerrUser

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
SESSION_COOKIE_NAME = "connect.sid"

MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


class SeerrError(RuntimeError):
    """Any failure talking to Seerr.

    ``status_code`` is ``None`` when the request never got a response.
    ``detail`` carries Seerr's own message where it gave one, so a quota
    rejection can be surfaced to the user verbatim instead of as "500".
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class SeerrAuthError(SeerrError):
    """The Plex token was rejected by Seerr — the user is not known to it."""


class SeerrClient:
    """Thin async wrapper over the Seerr v1 API."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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

    # ----------------------------------------------------------------- #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue a request with an explicitly controlled cookie header.

        The request is constructed directly rather than through
        ``client.request()`` on purpose. That path merges the client's cookie
        jar into every outgoing request, and Seerr's ``connect.sid`` lands in
        that jar from each ``/auth/plex`` response — so on a shared client, one
        user's Seerr session would be attached to the next user's request and
        their request would be filed under the wrong account. Building the
        request here means the jar is never consulted and each call carries
        exactly the session it was given, or none.
        """
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())

        request = httpx.Request(
            method,
            url,
            json=json,
            params=params,
            headers=headers,
            extensions={"timeout": self._timeout.as_dict()},
        )
        try:
            return await self.client.send(request)
        except httpx.HTTPError as exc:
            raise SeerrError(f"{method} {url} failed: {exc}") from exc

    @staticmethod
    def _detail_of(response: httpx.Response) -> str | None:
        """Pull Seerr's own error message out of a failed response, if there is one."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:300] or None
        if isinstance(body, dict):
            for key in ("message", "error", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    # ----------------------------------------------------------------- #

    async def authenticate_plex(self, plex_token: str) -> SeerrAuth:
        """Validate a Plex token and return the Seerr user it belongs to.

        Raises :class:`SeerrAuthError` when Seerr rejects the token (the user
        has no Seerr account, or the token is bad), and :class:`SeerrError` for
        anything else — an unreachable Seerr must not read as "bad token".
        """
        response = await self._request("POST", "auth/plex", json={"authToken": plex_token})

        if response.status_code in (401, 403):
            raise SeerrAuthError(
                "Seerr rejected the Plex token",
                status_code=response.status_code,
                detail=self._detail_of(response),
            )
        if response.status_code >= 400:
            raise SeerrError(
                f"Seerr auth failed with {response.status_code}",
                status_code=response.status_code,
                detail=self._detail_of(response),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SeerrError("Seerr auth returned a non-JSON body") from exc

        return SeerrAuth(
            user=SeerrUser.model_validate(payload),
            session_cookie=response.cookies.get(SESSION_COOKIE_NAME),
        )

    async def _as_user(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        """Call Seerr as the signed-in user and return the decoded body.

        Bodies are returned **verbatim**. These endpoints are a thin proxy: the
        clients already parse Seerr's own shapes, so reshaping them here would
        buy nothing and cost a client rewrite.
        """
        cookies = {SESSION_COOKIE_NAME: session_cookie} if session_cookie else None
        response = await self._request(method, path, params=params, json=json, cookies=cookies)

        if response.status_code >= 400:
            raise SeerrError(
                f"Seerr returned {response.status_code} for {method} {path}",
                status_code=response.status_code,
                detail=self._detail_of(response),
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def list_requests(
        self,
        *,
        session_cookie: str | None,
        take: int = 50,
        skip: int = 0,
        filter: str | None = None,
        sort: str | None = None,
    ) -> Any:
        """List requests as the user.

        Seerr scopes this itself: a caller without ``MANAGE_REQUESTS`` or
        ``REQUEST_VIEW`` sees only their own requests, which is exactly what a
        end-user client should get, while an admin sees everything. One endpoint
        serves both apps without branching here.
        """
        params: dict[str, Any] = {"take": take, "skip": skip}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        return await self._as_user("GET", "request", session_cookie=session_cookie, params=params)

    async def update_request_status(
        self, *, session_cookie: str | None, request_id: int, status: str
    ) -> Any:
        """Approve or decline a request. ``status`` is ``approve`` or ``decline``.

        Seerr guards this with ``MANAGE_REQUESTS``; callers should refuse
        non-admins before getting here rather than relying on the 403.
        """
        if status not in ("approve", "decline"):
            raise ValueError("status must be 'approve' or 'decline'")
        return await self._as_user(
            "POST", f"request/{request_id}/{status}", session_cookie=session_cookie
        )

    async def delete_request(self, *, session_cookie: str | None, request_id: int) -> Any:
        """Delete a request.

        Seerr allows a user to delete their own, and an admin to delete any.
        """
        return await self._as_user(
            "DELETE", f"request/{request_id}", session_cookie=session_cookie
        )

    async def get_current_user(self, *, session_cookie: str | None) -> Any:
        """``/auth/me`` — the signed-in user, for push registration."""
        return await self._as_user("GET", "auth/me", session_cookie=session_cookie)

    async def create_request(
        self,
        *,
        session_cookie: str | None,
        media_type: str,
        tmdb_id: int,
        seasons: Sequence[int] | None = None,
    ) -> SeerrRequestResult:
        """Create a request in Seerr as the authenticated user.

        ``tmdb_id`` is a TMDB id, not an IMDB id — Seerr's request endpoint is
        TMDB-keyed, unlike everything else in this service, which is IMDB-keyed
        because that is what Prowlarr search takes.  The client supplies the
        TMDB id directly from Plex metadata; we never resolve one to the other.

        ``seasons`` is passed through exactly as given for ``tv`` (Seerr treats
        ``0`` as specials, and the literal string ``"all"`` as every season
        except specials — the client sends explicit numbers, so we do not
        synthesise either form).
        """
        body: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == MEDIA_TYPE_TV:
            body["seasons"] = list(seasons or [])

        cookies = {SESSION_COOKIE_NAME: session_cookie} if session_cookie else None
        response = await self._request("POST", "request", json=body, cookies=cookies)

        if response.status_code >= 400:
            detail = self._detail_of(response)
            raise SeerrError(
                f"Seerr rejected the request ({response.status_code})",
                status_code=response.status_code,
                detail=detail,
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        raw = payload if isinstance(payload, dict) else {}
        return SeerrRequestResult(
            id=raw.get("id"),
            status=raw.get("status"),
            media_type=(raw.get("type") or raw.get("mediaType") or media_type),
            raw=raw,
        )
