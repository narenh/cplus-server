"""Is the aligner there, and is its runtime installed? Read from the shared directory."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from cplus_align import install as installer
from cplus_align.protocol import (
    HEARTBEAT_STALE,
    AlignPaths,
    align_dir,
    fresh,
    read_json,
    write_json,
)


@dataclass(frozen=True)
class RuntimeView:
    """The aligner as the Audiobooks tab describes it.

    ``status`` is one of:

    ``unconfigured``  no ``CPLUS_ALIGN_DIR`` — the deployment has no aligner
    ``offline``       configured, but the sidecar has not checked in recently
    ``absent``        up, runtime not installed (the "Enable" state)
    ``installing`` / ``removing``
    ``ready``         installed and current
    ``outdated``      installed for an older image; needs an update to run
    ``failed``        the last install or removal failed (``error`` says why)
    """

    status: str
    error: str | None = None
    progress: dict[str, Any] | None = None
    download_bytes: int = 0
    disk_bytes: int | None = None
    engine_running: bool = False
    installed: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def percent(self) -> float | None:
        progress = self.progress or {}
        total, done = progress.get("total"), progress.get("done")
        if not total or done is None:
            return None
        return max(0.0, min(100.0, 100.0 * done / total))


def paths() -> AlignPaths | None:
    root = align_dir()
    return AlignPaths(root) if root else None


def read_runtime(root: AlignPaths | None = None) -> RuntimeView:
    target = root or paths()
    if target is None:
        return RuntimeView("unconfigured")

    state = read_json(target.state_file) or {"status": "absent"}
    heartbeat = read_json(target.heartbeat_file)
    request = read_json(target.request_file)
    status = str(state.get("status") or "absent")
    installed = status == "ready"

    try:
        download = installer.download_size(paths=target)
    except (installer.InstallError, OSError, ValueError, KeyError):
        download = 0

    if request and status not in ("installing", "removing"):
        status = "installing" if request.get("action") == "install" else "removing"
    elif status == "ready" and state.get("runtime") != installer.runtime_id():
        status = "outdated"
    if not fresh(heartbeat, HEARTBEAT_STALE):
        status = "offline"

    return RuntimeView(
        status=status,
        error=state.get("error"),
        progress=state.get("progress"),
        download_bytes=download,
        disk_bytes=state.get("disk"),
        engine_running=bool(heartbeat and heartbeat.get("engine") == "running"),
        installed=installed,
    )


def request(target: AlignPaths, action: str) -> None:
    """Ask the sidecar to ``install`` or ``uninstall`` its runtime."""
    write_json(target.request_file, {"action": action, "t": time.time()})
