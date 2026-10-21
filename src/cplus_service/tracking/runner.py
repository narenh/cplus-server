"""The work of tracking one grab, from "sent to Prowlarr" to "playable in Plex".

One background task per grab walks the phases in order::

    finding_torrent → downloading_header → scanning → matching → ready
                                                                 ↘ failed (from any of them)

``finding_torrent``
    Look for the torrent in qBittorrent: by info hash when the release had one,
    otherwise the newest torrent added since just before the grab whose name is
    the release title. Prowlarr and qBittorrent each take a moment, so this
    polls for up to ~30 s. The hash found is written to the grab's row.

``downloading_header``
    Switch on sequential download and first/last piece priority, wait for a
    magnet link's metadata, pick the main file (the largest video file), and
    poll ``pieceStates`` about once a second until the pieces holding its first
    3 MB and its last piece are complete. Plex can identify a file from those.

``scanning``
    Ask Plex to scan just the release's folder, in the library whose folder
    holds it — or, if none does, every library of the release's kind.

``matching``
    Poll the library's most recently added items about once a second for one
    whose file is the main file, for up to ~60 s, scanning once more if it has
    not appeared after ~20 s. Its ``ratingKey`` is the answer.

Every way out of :meth:`TrackingJob.run` other than cancellation (which only
happens at shutdown) ends in ``ready`` or ``failed`` with a sentence the app can
show. A transient error from qBittorrent or Plex is retried a few times before
it counts; a refused password fails at once, since retrying cannot fix it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Config, Grab
from ..db.session import session_scope
from ..plex.client import PlexLibrarySection, PlexServerClient, PlexServerError
from ..qbittorrent.client import (
    QBittorrentAuthError,
    QBittorrentClient,
    QBittorrentError,
    Torrent,
)
from . import paths
from .state import MIB, GrabPhase, GrabProgress
from .torrent import (
    HeaderPlan,
    file_offset,
    header_pieces,
    main_video_file,
    match_by_hash,
    match_by_name,
    needs_last_piece,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: qBittorrent states that mean the download will not make progress by itself.
BROKEN_STATES = {"error", "missingFiles"}

_TV_RELEASE = re.compile(
    r"(?i)(?:^|[ ._\-\[(])(?:s\d{1,3}(?:e\d{1,4})?|season[ ._]?\d{1,3})(?:$|[ ._\-\])])"
)


class TrackingError(Exception):
    """Tracking cannot go on; ``str(exc)`` is the sentence the app shows."""


@dataclass(frozen=True)
class TrackingTimings:
    """How long each phase waits and how often it looks. Tests shrink these."""

    find_timeout: float = 30.0
    find_interval: float = 1.0
    #: How far before the grab a torrent's ``added_on`` may be and still count
    #: as this grab's. Generous, because it is qBittorrent's clock against this
    #: server's, on two different machines.
    added_slack_seconds: int = 300
    header_interval: float = 1.0
    header_timeout: float = 30 * 60.0
    match_interval: float = 1.0
    match_timeout: float = 60.0
    rescan_after: float = 20.0
    #: Consecutive failed calls to qBittorrent or Plex before tracking gives up.
    max_consecutive_errors: int = 5


@dataclass(frozen=True)
class TrackingSettings:
    """What a tracking task needs from the config row, read once when it starts."""

    qbittorrent_url: str
    qbittorrent_username: str | None
    qbittorrent_password: str | None
    plex_url: str
    plex_token: str

    @classmethod
    def from_config(cls, config: Config) -> TrackingSettings | None:
        """The settings, or ``None`` when tracking is off or cannot work."""
        if not tracking_enabled(config):
            return None
        assert config.qbittorrent_url and config.plex_server_base_url and config.plex_admin_token
        return cls(
            qbittorrent_url=config.qbittorrent_url,
            qbittorrent_username=config.qbittorrent_username,
            qbittorrent_password=config.qbittorrent_password,
            plex_url=config.plex_server_base_url,
            plex_token=config.plex_admin_token,
        )


def tracking_enabled(config: Config) -> bool:
    """Whether grabs are tracked: switched on, qBittorrent set, and Plex reachable.

    Plex counts because without the admin's server there is nothing to scan
    and no item to find — the app would be told to wait for an answer that
    cannot come. The server is connected by signing in to this console.
    """
    return bool(
        config.grab_tracking_enabled
        and config.qbittorrent_url
        and config.plex_admin_token
        and config.plex_server_base_url
    )


def guess_section_type(release_title: str) -> str:
    """``"show"`` for a release named like an episode or season, else ``"movie"``.

    Only consulted when no library's folder holds the download, to decide which
    libraries to scan instead.
    """
    return "show" if _TV_RELEASE.search(release_title) else "movie"


Persist = Callable[..., Awaitable[None]]


def persister(sessionmaker: async_sessionmaker[AsyncSession], grab_id: int) -> Persist:
    """A function writing tracking columns onto the grab's row.

    Best effort: a failed write is logged and tracking carries on, since the
    in-memory state is what the app is reading.
    """

    async def persist(**columns: Any) -> None:
        try:
            async with session_scope(sessionmaker) as session:
                grab = await session.get(Grab, grab_id)
                if grab is None:
                    return
                for name, value in columns.items():
                    setattr(grab, name, value)
        except Exception:
            logger.exception("could not record tracking state on grab %s", grab_id)

    return persist


class TrackingJob:
    """One grab's walk through the phases. See the module docstring."""

    def __init__(
        self,
        progress: GrabProgress,
        *,
        qbittorrent: QBittorrentClient,
        plex: PlexServerClient,
        timings: TrackingTimings,
        persist: Persist,
        hash_hints: Sequence[str],
        grabbed_at: float,
    ) -> None:
        self.progress = progress
        self.qbt = qbittorrent
        self.plex = plex
        self.timings = timings
        self.persist = persist
        self.hash_hints = [h for h in hash_hints if h]
        self.grabbed_at = grabbed_at
        self._errors = 0
        self._scans = 0
        #: Wall-clock moments each phase finished, for the timing summary.
        self._marks: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Logging
    #
    # One INFO line per step, each carrying the grab id, the title and the
    # seconds since the grab was sent, then one summary line per grab — so a
    # real grab can be timed from the production log alone. Nothing secret
    # appears: hashes are cut to 8 characters, and no token, password or
    # cookie is ever passed in.
    # ------------------------------------------------------------------ #

    def _elapsed(self, since: float | None = None) -> float:
        return time.time() - (self.grabbed_at if since is None else since)

    def _log(self, level: int, message: str, *args: object) -> None:
        logger.log(
            level,
            "grab %s +%.1fs \"%s\": " + message,
            self.progress.grab_id,
            self._elapsed(),
            self.progress.title,
            *args,
        )

    def _mark(self, name: str) -> None:
        self._marks[name] = time.time()

    def _span(self, start: str | None, end: str) -> str:
        """Seconds between two marks (``None`` is the grab itself), as text."""
        begin = self.grabbed_at if start is None else self._marks.get(start)
        finish = self._marks.get(end)
        if begin is None or finish is None:
            return "?"
        return f"{finish - begin:.1f} s"

    async def run(self) -> None:
        if self.hash_hints:
            how = "info hash " + ", ".join(h[:8] for h in self.hash_hints) + ", then by name"
        else:
            how = "no info hash, so by name"
        self._log(logging.INFO, "%s (%s)", GrabPhase.FINDING_TORRENT.value, how)
        try:
            torrent = await self.find_torrent()
            plan = await self.wait_for_header(torrent)
            sections = await self.scan(plan)
            rating_key = await self.match(plan, sections)
        except asyncio.CancelledError:
            raise
        except TrackingError as exc:
            await self._fail(str(exc))
        except Exception as exc:  # anything unforeseen must still end the phase
            logger.exception("grab %s: tracking failed unexpectedly", self.progress.grab_id)
            await self._fail(f"Tracking stopped unexpectedly ({type(exc).__name__}).")
        else:
            # The row first, so that once the app sees "ready" the row says so too.
            await self.persist(tracking_phase=GrabPhase.READY.value, rating_key=rating_key)
            self.progress.update(phase=GrabPhase.READY, rating_key=rating_key)
            self._log(logging.INFO, "ready, rating_key %s", rating_key)
            logger.info(
                "grab %s tracking done in %.1f s: torrent %s, header %s, scan→item %s,"
                " rating_key %s",
                self.progress.grab_id,
                self._elapsed(),
                self._span(None, "torrent"),
                self._span("torrent", "header"),
                self._span("scan", "item"),
                rating_key,
            )

    async def _fail(self, message: str) -> None:
        phase = self.progress.phase.value
        # The column holds 512 characters; a message naming a long path may not fit.
        await self.persist(tracking_phase=GrabPhase.FAILED.value, tracking_error=message[:512])
        self.progress.update(phase=GrabPhase.FAILED, error=message)
        self._log(logging.WARNING, "failed during %s: %s", phase, message)
        logger.warning(
            "grab %s tracking failed after %.1f s during %s: %s",
            self.progress.grab_id,
            self._elapsed(),
            phase,
            message,
        )

    async def _enter(self, phase: GrabPhase) -> None:
        self._log(logging.INFO, "%s", phase.value)
        await self.persist(tracking_phase=phase.value)
        self.progress.update(phase=phase)

    # ------------------------------------------------------------------ #
    # Calls that may fail transiently
    # ------------------------------------------------------------------ #

    def _transient(self, exc: Exception, service: str) -> None:
        """Count a failed call; raise :class:`TrackingError` once it is not transient."""
        if isinstance(exc, QBittorrentAuthError):
            raise TrackingError(str(exc)) from exc
        if isinstance(exc, PlexServerError) and exc.status_code in (401, 403):
            raise TrackingError(
                "Plex refused this server's token. Sign in to the cplus console again."
            ) from exc
        self._errors += 1
        self._log(logging.WARNING, "%s call failed (%d in a row): %s", service, self._errors, exc)
        if self._errors >= self.timings.max_consecutive_errors:
            raise TrackingError(f"Could not reach {service}.") from exc

    def _ok(self) -> None:
        self._errors = 0

    async def _call(self, service: str, call: Callable[[], Awaitable[T]], interval: float) -> T:
        """``call()``, retried through transient failures."""
        while True:
            try:
                result = await call()
            except (QBittorrentError, PlexServerError) as exc:
                self._transient(exc, service)
                await asyncio.sleep(interval)
                continue
            self._ok()
            return result

    # ------------------------------------------------------------------ #
    # finding_torrent
    # ------------------------------------------------------------------ #

    async def find_torrent(self) -> Torrent:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timings.find_timeout
        since = int(self.grabbed_at) - self.timings.added_slack_seconds
        torrents: list[Torrent] = []
        reached = False
        while True:
            try:
                torrents = await self.qbt.torrents(newest_first=True)
            except QBittorrentError as exc:
                self._transient(exc, "qBittorrent")
            else:
                self._ok()
                reached = True
                by_hash = match_by_hash(torrents, self.hash_hints)
                if by_hash is not None:
                    return await self._found(by_hash, "hash")
                by_name = match_by_name(torrents, self.progress.title, added_since=since)
                if by_name is not None:
                    return await self._found(by_name, "name")
            if loop.time() >= deadline:
                break
            await asyncio.sleep(self.timings.find_interval)

        if not reached:
            raise TrackingError("Could not reach qBittorrent.")
        # The grab may have been of a release qBittorrent already had, in which
        # case nothing new was added and the existing torrent is the right one.
        older = match_by_name(torrents, self.progress.title, added_since=None)
        if older is not None:
            return await self._found(older, "name, already in qBittorrent before the grab")
        raise TrackingError(
            "qBittorrent never showed this download. Check that the action's download"
            " client is the qBittorrent configured in cplus."
        )

    async def _found(self, torrent: Torrent, how: str) -> Torrent:
        self._mark("torrent")
        self._log(logging.INFO, "torrent found by %s: %s \"%s\"", how, torrent.hash[:8],
                  torrent.name)
        self.progress.update(torrent_hash=torrent.hash)
        self.progress.set_download(
            progress=torrent.progress, speed_bytes=torrent.dlspeed, eta=torrent.eta
        )
        await self.persist(torrent_hash=torrent.hash)
        return torrent

    # ------------------------------------------------------------------ #
    # downloading_header
    # ------------------------------------------------------------------ #

    async def _refresh_torrent(self, torrent_hash: str) -> Torrent:
        torrent = await self._call(
            "qBittorrent", lambda: self.qbt.torrent(torrent_hash), self.timings.header_interval
        )
        if torrent is None:
            raise TrackingError("The download was removed from qBittorrent.")
        if torrent.state in BROKEN_STATES:
            raise TrackingError(f"qBittorrent reports the download as {torrent.state}.")
        self.progress.set_download(
            progress=torrent.progress, speed_bytes=torrent.dlspeed, eta=torrent.eta
        )
        return torrent

    async def _plan(self, torrent: Torrent) -> HeaderPlan | None:
        """Work out which pieces matter, or ``None`` while metadata is still missing."""
        props = await self.qbt.properties(torrent.hash)
        files = await self.qbt.files(torrent.hash)
        if props.piece_size <= 0 or not files:
            return None
        main = main_video_file(files)
        if main is None:
            raise TrackingError("This download has no video file for Plex to play.")
        save_path = props.save_path or torrent.save_path
        if not save_path:
            return None
        pieces = header_pieces(main, props.piece_size, offset=file_offset(files, main),
                               include_last=needs_last_piece(main.name))
        file_path = paths.join(save_path, main.name)
        folder = paths.release_folder(save_path, main.name, multi_file=len(files) > 1)
        self._log(
            logging.INFO,
            "main file %s (%.1f MB); header needs %d piece(s) of %.2f MB: %s",
            main.name,
            main.size / MIB,
            len(pieces),
            props.piece_size / MIB,
            pieces,
        )
        return HeaderPlan(
            file=main,
            file_path=file_path,
            folder=folder,
            pieces=tuple(pieces),
            piece_size=props.piece_size,
        )

    async def wait_for_header(self, torrent: Torrent) -> HeaderPlan:
        await self._enter(GrabPhase.DOWNLOADING_HEADER)
        try:
            switched = await self.qbt.prioritise_start(torrent)
        except QBittorrentAuthError as exc:
            raise TrackingError(str(exc)) from exc
        except QBittorrentError as exc:
            # Not fatal: the header still arrives, only later.
            self._log(logging.WARNING, "could not prioritise the start of the file: %s", exc)
        else:
            self._log(
                logging.INFO,
                "switched on %s",
                " and ".join(switched) if switched else "nothing (both already on)",
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timings.header_timeout
        plan: HeaderPlan | None = None
        while True:
            current = await self._refresh_torrent(torrent.hash)
            try:
                if plan is None:
                    plan = await self._plan(current)
                if plan is not None:
                    states = await self.qbt.piece_states(current.hash)
                    self.progress.update(
                        header_have_bytes=plan.have_bytes(states),
                        header_need_bytes=plan.need_bytes,
                    )
                    if plan.ready(states):
                        self._mark("header")
                        self._log(
                            logging.INFO,
                            "header complete: %d of %d piece(s) on disk",
                            plan.complete(states),
                            len(plan.pieces),
                        )
                        return plan
            except QBittorrentError as exc:
                self._transient(exc, "qBittorrent")
            else:
                self._ok()
            if loop.time() >= deadline:
                minutes = round(self.timings.header_timeout / 60)
                raise TrackingError(
                    f"The start of the file had not downloaded after {minutes} minutes."
                    if plan is not None
                    else f"qBittorrent had no file list for this download after {minutes} minutes."
                )
            await asyncio.sleep(self.timings.header_interval)

    # ------------------------------------------------------------------ #
    # scanning
    # ------------------------------------------------------------------ #

    async def _refresh(self, sections: Sequence[PlexLibrarySection], folder: str | None) -> None:
        for section in sections:
            self._scans += 1
            self._log(
                logging.INFO,
                "scan %d: library \"%s\" (section %s), %s",
                self._scans,
                section.name,
                section.id,
                f"folder {folder}" if folder else "whole library",
            )
            await self._call(
                "Plex",
                lambda s=section: self.plex.refresh_section(s.id, path=folder),
                self.timings.match_interval,
            )

    async def scan(self, plan: HeaderPlan) -> tuple[list[PlexLibrarySection], str | None]:
        """Ask Plex to scan; returns the libraries to watch and the folder scanned."""
        await self._enter(GrabPhase.SCANNING)
        self._mark("scan")
        sections = await self._call(
            "Plex", self.plex.list_library_sections, self.timings.match_interval
        )
        section = paths.section_for_path(sections, plan.folder)
        if section is not None:
            await self._refresh([section], plan.folder)
            return [section], plan.folder

        kind = guess_section_type(self.progress.title)
        targets = [s for s in sections if s.type == kind]
        self._log(
            logging.WARNING,
            "no Plex library's folder holds %s; scanning every %s library instead",
            plan.folder,
            kind,
        )
        if not targets:
            raise TrackingError(
                f"No Plex library holds {plan.folder}, and there is no {kind} library to scan."
            )
        await self._refresh(targets, None)
        return targets, None

    # ------------------------------------------------------------------ #
    # matching
    # ------------------------------------------------------------------ #

    async def _find_item(
        self, sections: Sequence[PlexLibrarySection], file_path: str
    ) -> str | None:
        for section in sections:
            items = await self.plex.recent_items(section.id, section.type)
            for item in items:
                if paths.any_same_file(item.files, file_path):
                    return item.rating_key
        return None

    async def match(
        self, plan: HeaderPlan, scanned: tuple[list[PlexLibrarySection], str | None]
    ) -> str:
        sections, folder = scanned
        await self._enter(GrabPhase.MATCHING)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timings.match_timeout
        rescanned = False
        while True:
            try:
                rating_key = await self._find_item(sections, plan.file_path)
            except PlexServerError as exc:
                self._transient(exc, "Plex")
            else:
                self._ok()
                if rating_key is not None:
                    self._mark("item")
                    self._log(logging.INFO, "item found: rating_key %s", rating_key)
                    return rating_key
            # Keep the download numbers moving while Plex catches up.
            await self._refresh_torrent_quietly()
            now = loop.time()
            if not rescanned and now - started >= self.timings.rescan_after:
                rescanned = True
                self._log(logging.INFO, "not in Plex yet; scanning again")
                await self._refresh(sections, folder)
            if now >= deadline:
                raise TrackingError(
                    "Plex scanned the folder but did not add the file. It may not recognise"
                    " the release name."
                )
            await asyncio.sleep(self.timings.match_interval)

    async def _refresh_torrent_quietly(self) -> None:
        """Update the download numbers; a failure is not a reason to stop matching."""
        torrent_hash = self.progress.torrent_hash
        if not torrent_hash:
            return
        try:
            torrent = await self.qbt.torrent(torrent_hash)
        except QBittorrentError:
            return
        if torrent is not None:
            self.progress.set_download(
                progress=torrent.progress, speed_bytes=torrent.dlspeed, eta=torrent.eta
            )


def plex_client(settings: TrackingSettings, http: httpx.AsyncClient) -> PlexServerClient:
    return PlexServerClient(settings.plex_url, settings.plex_token, client=http)
