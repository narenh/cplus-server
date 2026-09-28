"""The shared directory cplus-service and the aligner sidecar talk through.

Both containers mount one volume (``CPLUS_ALIGN_DIR``, ``/align`` in the
image). There is no network API between them and no shared database: each
side writes only its own files, always atomically (write a temp file, then
rename), and reads the other side's files tolerantly — a missing or
half-written file reads as "nothing yet", never as an error.

Layout::

    runtime/              the sidecar's: downloaded runtime and model
      state.json          install state (supervisor writes)
      heartbeat.json      "the sidecar is alive" (supervisor writes, every few s)
      engine.json         which job the engine is on (engine writes)
      calibration.json    measured speed of past jobs, for ETAs (engine writes)
      venv/  model/  downloads/
    control/
      request.json        install / uninstall request (cplus writes)
    jobs/<id>/
      job.json            what to align (cplus writes, last, so it marks the job ready)
      book.epub           the uploaded epub (cplus writes)
      status.json         progress (engine writes)
      verify.json         verification verdict (engine writes)
      result.json         the finished alignment (engine writes)
      error.json          why it stopped (engine or supervisor writes)
      cancel              stop this job (cplus writes)
      work/               engine scratch: audio, decoded PCM, emissions

cplus-service never holds a file the sidecar is writing open, and the sidecar
never reads the database: the Prowlarr key and every Plex token stay in the
cplus container. Audio reaches the sidecar through
``/internal/aligner/jobs/{id}/tracks/{n}``, authorised by a per-job key that
lives in ``job.json`` and nowhere else.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

ALIGN_DIR_ENV = "CPLUS_ALIGN_DIR"

#: The heartbeat is rewritten every :data:`HEARTBEAT_INTERVAL` seconds; a reader
#: treats anything older than :data:`HEARTBEAT_STALE` as "the sidecar is not
#: running" — generous, so a busy host or a slow disk never reads as an outage.
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_STALE = 45.0

#: A running job rewrites its status at least this often even when nothing
#: moved, so "no update for N minutes" means the engine is stuck or gone rather
#: than merely in a long stage.
STATUS_KEEPALIVE = 20.0
STATUS_STALE = 180.0

JOB_FILE = "job.json"
EPUB_FILE = "book.epub"
STATUS_FILE = "status.json"
VERIFY_FILE = "verify.json"
RESULT_FILE = "result.json"
ERROR_FILE = "error.json"
CANCEL_FILE = "cancel"
CRASHES_FILE = "crashes.json"
WORK_DIR = "work"

#: The header the sidecar sends on every audio request.
KEY_HEADER = "X-Aligner-Key"


def align_dir() -> Path | None:
    """The shared directory, or ``None`` when this install has no aligner configured."""
    value = os.environ.get(ALIGN_DIR_ENV, "").strip()
    return Path(value) if value else None


class AlignPaths:
    """Every well-known path under the shared directory, in one place."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def state_file(self) -> Path:
        return self.runtime / "state.json"

    @property
    def heartbeat_file(self) -> Path:
        return self.runtime / "heartbeat.json"

    @property
    def engine_file(self) -> Path:
        return self.runtime / "engine.json"

    @property
    def calibration_file(self) -> Path:
        return self.runtime / "calibration.json"

    @property
    def venv(self) -> Path:
        return self.runtime / "venv"

    @property
    def model_dir(self) -> Path:
        return self.runtime / "model"

    @property
    def downloads(self) -> Path:
        return self.runtime / "downloads"

    @property
    def control(self) -> Path:
        return self.root / "control"

    @property
    def request_file(self) -> Path:
        return self.control / "request.json"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    def job(self, job_id: int | str) -> Path:
        return self.jobs / str(job_id)

    def job_ids(self) -> list[int]:
        """Every job directory with a numeric name, oldest (lowest id) first."""
        try:
            names = [entry.name for entry in self.jobs.iterdir() if entry.is_dir()]
        except FileNotFoundError:
            return []
        return sorted(int(name) for name in names if name.isdigit())


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically: a reader sees the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or ``None`` if it is missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def fresh(payload: dict[str, Any] | None, max_age: float, *, now: float | None = None) -> bool:
    """Whether a file's own ``t`` timestamp is within ``max_age`` seconds of now."""
    if not payload:
        return False
    stamp = payload.get("t")
    if not isinstance(stamp, int | float):
        return False
    return ((now if now is not None else time.time()) - stamp) <= max_age


def needs_verify(job: Path) -> bool:
    """Uploaded and not yet checked against its audio."""
    return (
        (job / JOB_FILE).exists()
        and not (job / VERIFY_FILE).exists()
        and not (job / ERROR_FILE).exists()
        and not (job / CANCEL_FILE).exists()
    )


def ready_to_align(job: Path) -> bool:
    """Verified, and neither finished nor stopped."""
    verdict = read_json(job / VERIFY_FILE)
    return (
        bool(verdict and verdict.get("ok"))
        and not (job / RESULT_FILE).exists()
        and not (job / ERROR_FILE).exists()
        and not (job / CANCEL_FILE).exists()
    )


def has_work(paths: AlignPaths) -> bool:
    return any(
        needs_verify(paths.job(job_id)) or ready_to_align(paths.job(job_id))
        for job_id in paths.job_ids()
    )
