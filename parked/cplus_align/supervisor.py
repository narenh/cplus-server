"""The sidecar's main process: ``python -m cplus_align``.

Standard library only, on the image's own Python. It does three things:

* **Heartbeat.** Rewrites ``runtime/heartbeat.json`` every few seconds, so
  cplus-service can tell "the aligner service isn't running" apart from "it is
  idle".
* **Install and uninstall**, when cplus-service drops a request in
  ``control/request.json`` (the Enable/Disable buttons on the Audiobooks tab).
* **Runs the engine** (:mod:`cplus_align.pipeline`, on the downloaded runtime)
  whenever there is work, at low CPU priority. The engine exits by itself when
  the queue empties, which releases its memory; if it dies instead — the
  container's memory limit is the usual reason — the job it was on is retried,
  and failed with an explanation after the third crash rather than looping.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import install as installer
from .protocol import (
    CRASHES_FILE,
    ERROR_FILE,
    HEARTBEAT_INTERVAL,
    STATUS_FILE,
    AlignPaths,
    align_dir,
    has_work,
    read_json,
    write_json,
)

MAX_CRASHES = 3
RESTART_BACKOFF = 15.0

#: The engine's CPU priority. Plex and the other sites on the host win any
#: contest for CPU; the aligner takes what is left.
ENGINE_NICE = 10


def log(message: str) -> None:
    print(f"[supervisor {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Supervisor:
    def __init__(self, paths: AlignPaths) -> None:
        self.paths = paths
        self.engine: subprocess.Popen[bytes] | None = None
        self.engine_started = 0.0
        self.next_start = 0.0
        self.task: threading.Thread | None = None
        self.stopping = False
        self._pythonpath = self._make_pythonpath()

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        return read_json(self.paths.state_file) or {"status": "absent"}

    def set_state(self, **fields: Any) -> None:
        state = self.state()
        state.update(fields)
        state["t"] = time.time()
        write_json(self.paths.state_file, state)

    def runtime_ready(self) -> bool:
        state = self.state()
        return (
            state.get("status") == "ready"
            and state.get("runtime") == installer.runtime_id()
            and (self.paths.venv / "bin" / "python").exists()
        )

    def heartbeat(self) -> None:
        write_json(
            self.paths.heartbeat_file,
            {
                "t": time.time(),
                "pid": os.getpid(),
                "engine": "running" if self.engine and self.engine.poll() is None else "idle",
                "runtime": installer.runtime_id(),
            },
        )

    # --------------------------------------------------------- install/remove

    def handle_request(self) -> None:
        if self.task and self.task.is_alive():
            return
        request = read_json(self.paths.request_file)
        if not request:
            return
        action = request.get("action")
        if action == "install":
            self.stop_engine()
            self.set_state(status="installing", error=None, progress={"label": "Starting"})
            self.task = threading.Thread(target=self._install, daemon=True)
            self.task.start()
        elif action == "uninstall":
            self.stop_engine()
            self.set_state(status="removing", progress=None)
            self.task = threading.Thread(target=self._uninstall, daemon=True)
            self.task.start()
        else:
            self.paths.request_file.unlink(missing_ok=True)

    def _install(self) -> None:
        try:
            result = installer.install(self.paths, lambda p: self.set_state(progress=p))
            self.set_state(
                status="ready", error=None, progress=None, installed_at=time.time(), **result
            )
            log("runtime installed")
        except Exception as exc:  # noqa: BLE001 - shown to the admin as-is
            log(traceback.format_exc())
            message = str(exc) if isinstance(exc, installer.InstallError) else repr(exc)
            self.set_state(status="failed", error=message, progress=None)
        finally:
            self.paths.request_file.unlink(missing_ok=True)

    def _uninstall(self) -> None:
        try:
            installer.uninstall(self.paths)
            self.set_state(status="absent", error=None, progress=None, runtime=None, disk=0)
            log("runtime removed")
        except Exception as exc:  # noqa: BLE001
            self.set_state(status="failed", error=f"Couldn't remove the runtime: {exc}")
        finally:
            self.paths.request_file.unlink(missing_ok=True)

    # ----------------------------------------------------------------- engine

    def _make_pythonpath(self) -> str:
        """A directory holding only this package, for the runtime's Python to import.

        Pointing the runtime at the image's whole site-packages would let the
        image's own versions of shared libraries shadow the runtime's.
        """
        folder = Path(tempfile.mkdtemp(prefix="cplus-align-path-"))
        (folder / "cplus_align").symlink_to(Path(__file__).resolve().parent)
        return str(folder)

    def start_engine(self) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": self._pythonpath,
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TRANSFORMERS_VERBOSITY": "error",
            "PYTHONWARNINGS": "ignore",
        }
        self.engine = subprocess.Popen(
            [
                "nice", "-n", str(ENGINE_NICE),
                str(self.paths.venv / "bin" / "python"), "-m", "cplus_align.pipeline",
            ],
            env=env,
        )  # fmt: skip
        self.engine_started = time.monotonic()
        log(f"engine started (pid {self.engine.pid})")

    def stop_engine(self, timeout: float = 20.0) -> None:
        if not self.engine or self.engine.poll() is not None:
            return
        self.engine.send_signal(signal.SIGTERM)
        try:
            self.engine.wait(timeout)
        except subprocess.TimeoutExpired:
            self.engine.kill()
            self.engine.wait()

    def check_engine(self) -> None:
        if self.engine is None:
            return
        code = self.engine.poll()
        if code is None:
            return
        self.engine = None
        if code == 0:
            log("engine finished")
            return
        log(f"engine exited with {code}")
        self.next_start = time.monotonic() + RESTART_BACKOFF
        marker = read_json(self.paths.engine_file) or {}
        job_id = marker.get("job")
        if job_id is None:
            return
        job = self.paths.job(job_id)
        if not job.exists():
            return
        crashes = (read_json(job / CRASHES_FILE) or {}).get("count", 0) + 1
        write_json(job / CRASHES_FILE, {"count": crashes, "t": time.time()})
        if crashes >= MAX_CRASHES:
            killed = code < 0 and -code == signal.SIGKILL
            reason = (
                "The aligner was killed while working on this book, probably for running "
                "out of memory. Raise the aligner's memory limit and try again."
                if killed
                else f"The aligner crashed {crashes} times on this book (exit {code})."
            )
            write_json(job / ERROR_FILE, {"t": time.time(), "code": "crashed", "message": reason})
            write_json(job / STATUS_FILE, {"t": time.time(), "phase": "failed", "pct": 0})
            log(f"job {job_id}: giving up after {crashes} crashes")

    # ------------------------------------------------------------------- loop

    def recover(self) -> None:
        """A restart mid-install or mid-removal: pick it back up, or say it stopped."""
        state = self.state()
        if (
            state.get("status") in ("installing", "removing")
            and not self.paths.request_file.exists()
        ):
            self.set_state(status="failed", error="Interrupted by a restart. Try again.")

    def run(self) -> None:
        for folder in (self.paths.runtime, self.paths.control, self.paths.jobs):
            folder.mkdir(parents=True, exist_ok=True)
        self.recover()
        log(f"watching {self.paths.root}")
        last_beat = 0.0
        while not self.stopping:
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_INTERVAL:
                self.heartbeat()
                last_beat = now
            try:
                self.handle_request()
                self.check_engine()
                busy = self.task is not None and self.task.is_alive()
                if (
                    not busy
                    and self.engine is None
                    and now >= self.next_start
                    and self.runtime_ready()
                    and has_work(self.paths)
                ):
                    self.start_engine()
            except Exception:  # noqa: BLE001 - the loop must survive a bad file
                log(traceback.format_exc())
            time.sleep(1.0)
        self.stop_engine()


def main() -> None:
    root = align_dir()
    if root is None:
        sys.exit("CPLUS_ALIGN_DIR is not set; the aligner has nowhere to work.")
    supervisor = Supervisor(AlignPaths(root))

    def stop(_signum: int, _frame: Any) -> None:
        supervisor.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    supervisor.run()
