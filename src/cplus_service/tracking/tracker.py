"""The process-wide registry of tracked grabs and the tasks doing the tracking.

One :class:`GrabTracker` hangs off :class:`~cplus_service.api.state.AppState`.
It starts a task per grab, keeps each grab's :class:`~.state.GrabProgress`
for the long poll to read, and cancels whatever is still running at shutdown.

Finished grabs are kept for an hour (the app has long since stopped polling by
then) and at most a few hundred are kept at all; past that, ``GET /grabs/{id}``
answers from what was written to the grab's row instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..qbittorrent.client import QBittorrentClient
from .runner import TrackingJob, TrackingSettings, TrackingTimings, persister, plex_client
from .state import GrabProgress

logger = logging.getLogger(__name__)

#: How long a finished grab's state stays in memory.
RETAIN_FINISHED_SECONDS = 3600.0

#: The most grabs held in memory, finished or not.
MAX_RETAINED = 500


class GrabTracker:
    """Starts, holds and stops grab tracking for the whole process."""

    def __init__(self, timings: TrackingTimings | None = None) -> None:
        self.timings = timings or TrackingTimings()
        self._grabs: dict[int, GrabProgress] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._qbittorrent: QBittorrentClient | None = None

    def get(self, grab_id: int) -> GrabProgress | None:
        return self._grabs.get(grab_id)

    @property
    def running(self) -> int:
        return len(self._tasks)

    def qbittorrent(
        self, settings: TrackingSettings, http: httpx.AsyncClient
    ) -> QBittorrentClient:
        """The shared qBittorrent client for these settings.

        Shared so its session cookie is reused across grabs rather than signing
        in once per grab; rebuilt when the settings change.
        """
        current = self._qbittorrent
        if current is None or not current.matches(
            settings.qbittorrent_url, settings.qbittorrent_username, settings.qbittorrent_password
        ):
            current = QBittorrentClient(
                settings.qbittorrent_url,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
                client=http,
            )
            self._qbittorrent = current
        return current

    def start(
        self,
        *,
        grab_id: int,
        user_id: int,
        title: str,
        hash_hints: Sequence[str],
        grabbed_at: float,
        settings: TrackingSettings,
        sessionmaker: async_sessionmaker[AsyncSession],
        qbittorrent_http: httpx.AsyncClient,
        plex_http: httpx.AsyncClient,
    ) -> GrabProgress:
        """Begin tracking a grab whose row is already committed."""
        self._prune()
        progress = GrabProgress(grab_id=grab_id, user_id=user_id, title=title)
        self._grabs[grab_id] = progress
        job = TrackingJob(
            progress,
            qbittorrent=self.qbittorrent(settings, qbittorrent_http),
            plex=plex_client(settings, plex_http),
            timings=self.timings,
            persist=persister(sessionmaker, grab_id),
            hash_hints=hash_hints,
            grabbed_at=grabbed_at,
        )
        task = asyncio.create_task(job.run(), name=f"grab-tracking-{grab_id}")
        self._tasks[grab_id] = task

        def _done(finished: asyncio.Task[None], grab_id: int = grab_id) -> None:
            if self._tasks.get(grab_id) is finished:
                del self._tasks[grab_id]
            if not finished.cancelled() and finished.exception() is not None:
                # TrackingJob.run catches everything; this is the backstop.
                logger.error("grab %s: tracking task died", grab_id,
                             exc_info=finished.exception())

        task.add_done_callback(_done)
        return progress

    def _prune(self) -> None:
        now = time.monotonic()
        for grab_id, progress in list(self._grabs.items()):
            if (
                progress.finished_at is not None
                and now - progress.finished_at > RETAIN_FINISHED_SECONDS
            ):
                del self._grabs[grab_id]
        if len(self._grabs) >= MAX_RETAINED:
            finished = sorted(
                (p for p in self._grabs.values() if p.finished_at is not None),
                key=lambda p: p.finished_at or 0.0,
            )
            for progress in finished[: len(self._grabs) - MAX_RETAINED + 1]:
                del self._grabs[progress.grab_id]

    async def aclose(self) -> None:
        """Cancel every running task and wait for them to stop."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
