"""A progress bar that means something over a multi-hour job.

The book's duration is known before anything runs (Plex reports it), so every
stage can be sized up front from how fast this machine did it last time
(``runtime/calibration.json``), falling back to measured defaults on the first
job. While a stage runs, its own observed rate replaces the estimate. The bar
never moves backwards even when an estimate is revised upward.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..protocol import STATUS_KEEPALIVE, read_json, write_json

#: Seconds of work per second of audio, before this machine has finished a job.
#: The emissions figure is 3 threads of a 2.1 GHz Xeon at 15 s windows; the rest
#: are small next to it.
DEFAULT_RATES = {
    "decode": 0.004,
    "emissions": 0.2,
    "anchors": 0.002,
    "align": 0.02,
}
DEFAULT_DOWNLOAD_BPS = 4_000_000

STAGES = [
    ("download", "Downloading audio"),
    ("decode", "Decoding audio"),
    ("emissions", "Listening to the audio"),
    ("anchors", "Finding the text in the audio"),
    ("align", "Aligning sentences"),
    ("save", "Saving"),
]
LABELS = dict(STAGES)


def load_calibration(path: Path) -> dict[str, Any]:
    return read_json(path) or {}


def emissions_key(threads: int) -> str:
    return f"emissions@{threads}"


def predicted_seconds(
    calibration: dict[str, Any], *, duration: float, size: int, threads: int
) -> dict[str, float]:
    """How long each stage should take for a book of this length on this machine."""
    rates = {**DEFAULT_RATES, **{k: v for k, v in calibration.items() if k in DEFAULT_RATES}}
    emissions = calibration.get(emissions_key(threads))
    if emissions is None:
        # No history at this thread count: scale the default, measured at 3
        # threads (the exponent fits the measured 2- and 4-thread rates).
        emissions = DEFAULT_RATES["emissions"] * (3 / max(threads, 1)) ** 0.85
    bps = calibration.get("download_bps") or DEFAULT_DOWNLOAD_BPS
    return {
        "download": size / bps,
        "decode": duration * rates["decode"],
        "emissions": duration * emissions,
        "anchors": duration * rates["anchors"],
        "align": duration * rates["align"],
        "save": 5.0,
    }


class JobProgress:
    """Tracks one alignment job's stages and writes its ``status.json``."""

    def __init__(
        self,
        status_path: Path,
        predicted: dict[str, float],
        *,
        clock: Any = time.monotonic,
        wall: Any = time.time,
    ) -> None:
        self.path = status_path
        self.predicted = dict(predicted)
        self.actual: dict[str, float] = {}
        self.skipped: set[str] = set()
        self.clock = clock
        self.wall = wall
        self.stage: str | None = None
        self.stage_start = 0.0
        self.frac0 = 0.0
        self.frac = 0.0
        self.pct = 0.0
        self.message = ""
        self.last_write = -1e9
        self.last_pct_written = -1.0

    def start(self, stage: str, *, frac: float = 0.0, message: str = "") -> None:
        if self.stage and self.stage != stage and self.stage not in self.actual:
            self.actual[self.stage] = self.clock() - self.stage_start
        self.stage = stage
        self.stage_start = self.clock()
        self.frac0 = self.frac = frac
        self.message = message
        self.write(force=True)

    def finish_stage(self) -> None:
        if self.stage:
            self.actual[self.stage] = self.clock() - self.stage_start
            self.frac = 1.0

    def skip(self, stage: str) -> None:
        """A stage an earlier run of this job already finished.

        It takes no time now, but it is still part of the job: the bar counts it
        as done work, so a job resumed after a restart picks up at the percent
        it had reached instead of starting the bar again from zero.
        """
        self.actual[stage] = 0.0
        self.skipped.add(stage)

    def _current_full(self) -> float:
        """Seconds the whole current stage takes, start to finish, as best known now."""
        assert self.stage is not None
        prior = self.predicted[self.stage]
        elapsed = self.clock() - self.stage_start
        moved = self.frac - self.frac0
        if moved >= 0.02 and elapsed >= 10:
            live = elapsed / moved
            # Lean on the live rate as the stage advances.
            weight = min(1.0, moved * 5)
            return weight * live + (1 - weight) * prior
        return prior

    def estimate(self) -> tuple[float, float]:
        """-> (overall percent, seconds remaining)."""
        if self.stage is None:
            return 0.0, sum(self.predicted.values())
        names = [name for name, _ in STAGES]
        here = names.index(self.stage)

        def cost(name: str) -> float:
            if name in self.skipped:
                return self.predicted[name]
            return self.actual.get(name, self.predicted[name])

        before = sum(cost(name) for name in names[:here])
        full = self._current_full()
        future = sum(self.predicted[name] for name in names[here + 1 :])
        total = before + full + future
        remaining = (1 - self.frac) * full + future
        pct = 100.0 * (total - remaining) / total if total > 0 else 0.0
        return pct, remaining

    def update(self, frac: float, message: str | None = None) -> None:
        self.frac = min(max(frac, self.frac0), 1.0)
        if message is not None:
            self.message = message
        self.write()

    def write(self, *, force: bool = False, phase: str = "running") -> None:
        now = self.clock()
        pct, eta = self.estimate()
        pct = max(pct, self.pct)  # never backwards
        self.pct = pct
        if not force:
            since = now - self.last_write
            if since < 2.0:
                return
            if abs(pct - self.last_pct_written) < 0.1 and since < STATUS_KEEPALIVE:
                return
        self.last_write = now
        self.last_pct_written = pct
        write_json(
            self.path,
            {
                "t": self.wall(),
                "phase": phase,
                "stage": self.stage,
                "label": LABELS.get(self.stage or "", ""),
                "pct": round(min(pct, 99.9), 1),
                "eta": round(eta),
                "message": self.message,
            },
        )


def record_calibration(
    path: Path,
    progress: JobProgress,
    *,
    duration: float,
    size: int,
    threads: int,
    resumed: set[str],
) -> None:
    """Fold this job's measured stage speeds into the calibration file."""
    calibration = load_calibration(path)

    def blend(key: str, value: float) -> None:
        old = calibration.get(key)
        calibration[key] = value if old is None else round(0.5 * old + 0.5 * value, 6)

    if duration > 0:
        for stage in ("decode", "emissions", "anchors", "align"):
            seconds = progress.actual.get(stage)
            if not seconds or stage in resumed:
                continue
            key = emissions_key(threads) if stage == "emissions" else stage
            blend(key, seconds / duration)
    seconds = progress.actual.get("download")
    if seconds and size and "download" not in resumed and seconds > 1:
        blend("download_bps", size / seconds)
    write_json(path, calibration)
