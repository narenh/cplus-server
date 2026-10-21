"""One grab's tracking state, and waiting for it to change.

In memory, by design: a grab is tracked for a minute or two, the app polls
while it watches, and restarts are rare. The phase, torrent hash, rating key
and error are also written to the grab's row as they change (see
:mod:`.runner`), so the answer survives a restart even though the live numbers
do not.

Every change bumps :attr:`GrabProgress.version`, and a long poll
(``GET /grabs/{id}?since=…&wait=…``) waits on an :class:`asyncio.Event` that is
replaced, and the old one set, on each bump. A waiter therefore wakes for any
change after it started waiting, never misses one between checking the version
and starting to wait, and never wakes for nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..qbittorrent.client import ETA_UNKNOWN

MIB = 1024 * 1024


class GrabPhase(StrEnum):
    """Where a tracked grab is, in the order it moves through them."""

    FINDING_TORRENT = "finding_torrent"
    DOWNLOADING_HEADER = "downloading_header"
    SCANNING = "scanning"
    MATCHING = "matching"
    READY = "ready"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (GrabPhase.READY, GrabPhase.FAILED)


#: What the Grabs page prints for each phase.
PHASE_LABELS: dict[GrabPhase, str] = {
    GrabPhase.FINDING_TORRENT: "finding torrent",
    GrabPhase.DOWNLOADING_HEADER: "downloading start",
    GrabPhase.SCANNING: "scanning",
    GrabPhase.MATCHING: "waiting for Plex",
    GrabPhase.READY: "in Plex",
    GrabPhase.FAILED: "failed",
}


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / MIB, 1)


@dataclass
class GrabProgress:
    """Everything the app is told about one grab, plus the means to wait on it."""

    grab_id: int
    user_id: int
    title: str
    phase: GrabPhase = GrabPhase.FINDING_TORRENT
    version: int = 1
    torrent_hash: str | None = None
    header_have_bytes: int | None = None
    header_need_bytes: int | None = None
    progress: float | None = None
    speed_bytes: int | None = None
    eta_seconds: int | None = None
    rating_key: str | None = None
    error: str | None = None
    finished_at: float | None = None
    """``time.monotonic()`` when the phase became final, for eviction."""

    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def update(self, **changes: Any) -> bool:
        """Apply ``changes``; bump the version and wake waiters if anything differed.

        A finished grab is frozen: nothing moves it out of ``ready`` or
        ``failed``, so a late write from a task being torn down cannot undo
        the answer the app already acted on.
        """
        if self.phase.finished:
            return False
        changed = False
        for name, value in changes.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if not changed:
            return False
        if self.phase.finished:
            self.finished_at = time.monotonic()
        self.version += 1
        event, self._changed = self._changed, asyncio.Event()
        event.set()
        return True

    def set_download(self, *, progress: float, speed_bytes: int, eta: int) -> bool:
        return self.update(
            progress=round(min(max(progress, 0.0), 1.0), 4),
            speed_bytes=max(speed_bytes, 0),
            eta_seconds=None if eta >= ETA_UNKNOWN or eta < 0 else eta,
        )

    async def wait_newer(self, since: int, timeout: float) -> None:
        """Return once :attr:`version` is greater than ``since``, or after ``timeout``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while self.version <= since:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            event = self._changed
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                return

    def as_json(self) -> dict[str, Any]:
        """The ``GET /grabs/{id}`` body."""
        speed = self.speed_bytes
        return {
            "grab_id": self.grab_id,
            "version": self.version,
            "phase": self.phase.value,
            "title": self.title,
            "header": {
                "have_mb": _mb(self.header_have_bytes),
                "need_mb": _mb(self.header_need_bytes),
            },
            "download": {
                "progress": self.progress,
                # Megabits per second, as the name says: bytes × 8 / 10⁶.
                "speed_mbps": None if speed is None else round(speed * 8 / 1_000_000, 2),
                "eta_seconds": self.eta_seconds,
            },
            "rating_key": self.rating_key,
            "error": self.error,
        }
