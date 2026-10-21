"""Async client for the parts of qBittorrent's Web API v2 that grab tracking uses.

Grabs are still sent the way they always were — Prowlarr hands the release to
whichever download client the action names. This client only *finds* the
torrent afterwards and watches it: which pieces of the main file are on disk,
how fast it is coming, and two switches that make the start of the file arrive
first. It never adds, removes or moves a torrent.

**Authentication is a session cookie.** ``POST /api/v2/auth/login`` answers
``Ok.`` with a ``SID`` cookie, or ``Fails.`` — under a 200 either way — for a
wrong password. Every later call carries the cookie; qBittorrent forgets
sessions after a while (an hour of inactivity by default, or on restart) and
then answers 403, which is the cue to sign in again and retry once.

The cookie is held on this object and sent by hand rather than left to the
``httpx`` cookie jar. The jar applies browser cookie rules, which refuse or
mis-scope a cookie from a bare host name (``http://qbittorrent:8080`` in a
compose file) or a plain IP in some cases; reading ``Set-Cookie`` directly
works the same for all of them. An explicit ``Cookie`` header also wins over
anything the shared client's jar might hold.

**Nothing secret is ever logged or put in an exception message**: not the
password, and not the session cookie, which is as good as the password for as
long as it lives.

A WebUI with "Bypass authentication for clients on localhost / in whitelisted
IP subnets" switched on needs no credentials at all; leave both blank and this
client never signs in.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: What qBittorrent reports as ``eta`` when it has no estimate (100 days).
ETA_UNKNOWN = 8_640_000

#: ``pieceStates`` values.
PIECE_MISSING = 0
PIECE_DOWNLOADING = 1
PIECE_COMPLETE = 2


class QBittorrentError(RuntimeError):
    """Any failure talking to qBittorrent.

    ``status_code`` is ``None`` when the request never got a response. The
    message names the URL and status but never a credential.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QBittorrentAuthError(QBittorrentError):
    """qBittorrent refused the username and password, or has banned this IP."""


@dataclass(frozen=True)
class Torrent:
    """One entry of ``torrents/info`` — only the fields grab tracking reads."""

    hash: str
    name: str
    added_on: int
    progress: float
    dlspeed: int
    eta: int
    state: str
    seq_dl: bool | None
    """None when qBittorrent's answer leaves the field out: unknown, not off."""
    f_l_piece_prio: bool | None
    save_path: str
    infohash_v1: str
    infohash_v2: str
    completed: int = 0
    """Bytes downloaded and checked so far."""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Torrent:
        return cls(
            hash=str(raw.get("hash") or "").lower(),
            name=str(raw.get("name") or ""),
            added_on=int(raw.get("added_on") or 0),
            progress=float(raw.get("progress") or 0.0),
            dlspeed=int(raw.get("dlspeed") or 0),
            eta=int(raw.get("eta") or ETA_UNKNOWN),
            state=str(raw.get("state") or ""),
            seq_dl=_flag(raw.get("seq_dl")),
            f_l_piece_prio=_flag(raw.get("f_l_piece_prio")),
            save_path=str(raw.get("save_path") or ""),
            infohash_v1=str(raw.get("infohash_v1") or "").lower(),
            infohash_v2=str(raw.get("infohash_v2") or "").lower(),
            completed=int(raw.get("completed") or 0),
        )

    @property
    def hashes(self) -> set[str]:
        """Every hash this torrent answers to: its id, and its v1 and v2 info hashes.

        ``hash`` is qBittorrent's torrent id — the v1 info hash for a v1 or
        hybrid torrent, a truncated v2 hash for a v2-only one — so a release
        whose hash Prowlarr gave in the other form is still recognised.
        """
        return {h for h in (self.hash, self.infohash_v1, self.infohash_v2) if h}


@dataclass(frozen=True)
class TorrentProperties:
    """The slice of ``torrents/properties`` needed to locate a file's pieces.

    ``piece_size`` is ``0`` (qBittorrent reports ``-1``) until a magnet link's
    metadata has arrived; nothing about the files is known before then.
    """

    save_path: str
    piece_size: int
    pieces_num: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TorrentProperties:
        piece_size = int(raw.get("piece_size") or 0)
        pieces_num = int(raw.get("pieces_num") or 0)
        return cls(
            save_path=str(raw.get("save_path") or ""),
            piece_size=max(piece_size, 0),
            pieces_num=max(pieces_num, 0),
        )


@dataclass(frozen=True)
class TorrentFile:
    """One entry of ``torrents/files``.

    ``name`` is relative to the torrent's save path and, for a multi-file
    torrent, starts with the torrent's own folder. ``piece_range`` is the first
    and last piece (inclusive) holding any of the file.
    """

    index: int
    name: str
    size: int
    progress: float
    piece_range: tuple[int, int]

    @classmethod
    def from_api(cls, raw: dict[str, Any], position: int) -> TorrentFile:
        piece_range = raw.get("piece_range") or [0, 0]
        first, last = (int(piece_range[0]), int(piece_range[-1])) if piece_range else (0, 0)
        return cls(
            index=int(raw.get("index", position)),
            name=str(raw.get("name") or ""),
            size=int(raw.get("size") or 0),
            progress=float(raw.get("progress") or 0.0),
            piece_range=(first, last),
        )


def _describe(exc: httpx.HTTPError) -> str:
    """A transport failure in words. Request bodies — the login form — never appear."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _flag(value: Any) -> bool | None:
    """A torrent's on/off setting, or None when the answer doesn't carry it."""
    return None if value is None else bool(value)


def _session_cookie(response: httpx.Response) -> str | None:
    """The session cookie from a login response's ``Set-Cookie`` headers, as
    ``name=value``, if any.

    Older qBittorrent calls it ``SID``; newer releases name it per port, e.g.
    ``QBT_SID_8080``. Kept whole, so requests send back whichever it was.
    """
    for header in response.headers.get_list("set-cookie"):
        first = header.split(";", 1)[0].strip()
        name, _, value = first.partition("=")
        name = name.strip()
        if (name == "SID" or name.startswith("QBT_SID")) and value:
            return f"{name}={value.strip()}"
    return None


class QBittorrentClient:
    """Thin async wrapper over qBittorrent's Web API v2.

    Built with an externally owned ``httpx.AsyncClient`` in the app (one shared
    for the process, see :class:`~cplus_service.api.state.AppState`), or owns
    one when used as an async context manager::

        async with QBittorrentClient(url, user, password) as qbt:
            version = await qbt.app_version()

    Keep one instance per set of credentials: it holds the session cookie, so a
    fresh instance per call would sign in every time.
    """

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or ""
        self._password = password or ""
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._sid: str | None = None

    def __repr__(self) -> str:  # never show the password
        return f"QBittorrentClient({self.base_url!r}, username={self.username!r})"

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

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or self._password)

    def matches(self, base_url: str, username: str | None, password: str | None) -> bool:
        """Whether this instance was built from exactly these settings."""
        return (
            self.base_url == base_url.rstrip("/")
            and self.username == (username or "")
            and self._password == (password or "")
        )

    # ----------------------------------------------------------------- #
    # Transport
    # ----------------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v2/{path.lstrip('/')}"

    async def login(self) -> None:
        """Sign in and keep the session cookie.

        Raises :class:`QBittorrentAuthError` for a wrong username or password
        and for an IP qBittorrent has banned after too many failures — the two
        are told apart in the message, since only the second fixes itself.
        """
        url = self._url("auth/login")
        try:
            response = await self.client.post(
                url,
                data={"username": self.username, "password": self._password},
                # qBittorrent's CSRF check compares Referer/Origin to its own
                # host when either is present; sending its own URL satisfies it
                # behind proxies that add one.
                headers={"Referer": self.base_url},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"POST {url} failed: {_describe(exc)}") from exc

        if response.status_code == 403:
            raise QBittorrentAuthError(
                "qBittorrent has temporarily banned this address after too many failed"
                " sign-ins. Wait, then check the username and password.",
                status_code=403,
            )
        # Older qBittorrent answers a 200 "Ok." or "Fails."; newer releases answer
        # an empty 204 when signed in and a 401 when not.
        answer = response.text.strip()
        if response.status_code == 401 or answer == "Fails.":
            raise QBittorrentAuthError("qBittorrent rejected the username or password.")
        if response.status_code >= 400:
            raise QBittorrentError(
                f"POST {url} returned {response.status_code}",
                status_code=response.status_code,
            )
        if answer not in ("Ok.", ""):
            # Not qBittorrent's own answer at all: usually a proxy's sign-in page or
            # the wrong address. Say what came back, which no password is part of.
            kind = response.headers.get("content-type", "unknown type").split(";")[0]
            snippet = " ".join(answer[:80].split())
            raise QBittorrentAuthError(
                f"qBittorrent's sign-in gave an unexpected answer (HTTP {response.status_code},"
                f" {kind}: {snippet!r}). Check the URL is qBittorrent's Web UI itself,"
                " not a proxy or another page."
            )
        # No cookie with a successful answer: qBittorrent is letting this address
        # in without signing in ("Bypass authentication for clients in whitelisted
        # IP subnets", which a reverse proxy on the LAN often falls under). Carry
        # on without one; if a request is then refused, that 403 says so.
        self._sid = _session_cookie(response) or ""
        logger.debug("signed in to qBittorrent at %s", self.base_url)

    async def _send(
        self, method: str, url: str, *, params: dict[str, Any] | None, data: dict[str, Any] | None
    ) -> httpx.Response:
        headers = {"Referer": self.base_url}
        if self._sid:
            headers["Cookie"] = self._sid
        try:
            return await self.client.request(
                method, url, params=params, data=data, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"{method} {url} failed: {_describe(exc)}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = self._url(path)
        if self._sid is None and self.has_credentials:
            await self.login()
        response = await self._send(method, url, params=params, data=data)
        if response.status_code == 403 and self.has_credentials:
            # The session expired (or qBittorrent restarted): sign in again once.
            self._sid = None
            await self.login()
            response = await self._send(method, url, params=params, data=data)
        if response.status_code == 403:
            raise QBittorrentAuthError(
                "qBittorrent refused the request. Set a username and password, or allow"
                " this address to bypass authentication in qBittorrent's Web UI settings.",
                status_code=403,
            )
        if response.status_code >= 400:
            raise QBittorrentError(
                f"{method} {url} returned {response.status_code}",
                status_code=response.status_code,
            )
        return response

    async def _json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise QBittorrentError(f"GET {self._url(path)} returned a non-JSON body") from exc

    # ----------------------------------------------------------------- #
    # API surface
    # ----------------------------------------------------------------- #

    async def app_version(self) -> str:
        """qBittorrent's version, e.g. ``v4.6.7`` — backs the Verify button."""
        return (await self._request("GET", "app/version")).text.strip()

    async def webapi_version(self) -> str:
        return (await self._request("GET", "app/webapiVersion")).text.strip()

    async def torrents(
        self, *, hashes: Sequence[str] | None = None, newest_first: bool = False
    ) -> list[Torrent]:
        """``torrents/info``: every torrent, or only those with ``hashes``."""
        params: dict[str, Any] = {}
        if hashes:
            params["hashes"] = "|".join(h.lower() for h in hashes)
        if newest_first:
            params["sort"] = "added_on"
            params["reverse"] = "true"
        payload = await self._json("torrents/info", params=params)
        if not isinstance(payload, list):
            raise QBittorrentError("torrents/info did not return a list")
        return [Torrent.from_api(raw) for raw in payload if isinstance(raw, dict)]

    async def torrent(self, torrent_hash: str) -> Torrent | None:
        """One torrent by hash, or ``None`` once it is no longer in qBittorrent."""
        found = await self.torrents(hashes=[torrent_hash])
        return found[0] if found else None

    async def properties(self, torrent_hash: str) -> TorrentProperties:
        payload = await self._json("torrents/properties", params={"hash": torrent_hash})
        return TorrentProperties.from_api(payload if isinstance(payload, dict) else {})

    async def files(self, torrent_hash: str) -> list[TorrentFile]:
        """The torrent's files; empty until a magnet link's metadata has arrived."""
        payload = await self._json("torrents/files", params={"hash": torrent_hash})
        if not isinstance(payload, list):
            return []
        return [
            TorrentFile.from_api(raw, position)
            for position, raw in enumerate(payload)
            if isinstance(raw, dict)
        ]

    async def piece_states(self, torrent_hash: str) -> list[int]:
        """One int per piece: 0 missing, 1 downloading, 2 complete."""
        payload = await self._json("torrents/pieceStates", params={"hash": torrent_hash})
        if not isinstance(payload, list):
            return []
        return [int(state) for state in payload]

    async def toggle_sequential_download(self, torrent_hash: str) -> None:
        """Flip sequential download. A toggle — see :meth:`prioritise_start`."""
        await self._request(
            "POST", "torrents/toggleSequentialDownload", data={"hashes": torrent_hash}
        )

    async def toggle_first_last_piece_prio(self, torrent_hash: str) -> None:
        """Flip first/last piece priority. A toggle — see :meth:`prioritise_start`."""
        await self._request(
            "POST", "torrents/toggleFirstLastPiecePrio", data={"hashes": torrent_hash}
        )

    async def prioritise_start(self, torrent: Torrent) -> list[str]:
        """Switch on sequential download and first/last piece priority.

        Both endpoints *toggle*, so calling them blindly would switch off a
        setting the user (or a category default) had already turned on. Each
        is flipped only when ``torrent`` — read fresh from ``torrents/info`` —
        says it is off; a setting the answer leaves out is left alone, since
        flipping an unknown could just as well switch it off. Returns the names
        of the settings actually switched.
        """
        switched: list[str] = []
        if torrent.seq_dl is False:
            await self.toggle_sequential_download(torrent.hash)
            switched.append("sequential download")
        if torrent.f_l_piece_prio is False:
            await self.toggle_first_last_piece_prio(torrent.hash)
            switched.append("first/last piece priority")
        return switched
