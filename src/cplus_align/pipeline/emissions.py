"""The model, and running it over a whole book without holding the result in memory.

Emissions go straight into a ``.npy`` file on disk, window by window, with a
small JSON file recording how many windows are done. That file is the
checkpoint: a container restart six hours into a long book resumes at the next
window instead of starting again.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..protocol import write_json
from .audio import SR

#: Seconds of audio per model call, plus context either side that is computed
#: and thrown away so no word is cut at a window edge. 15 s measured faster than
#: 30 s on CPU and needs ~330 MB less working memory (0.78 GB against 1.1 GB,
#: on top of the 1.3 GB model).
WINDOW = int(os.environ.get("CPLUS_ALIGN_WINDOW", "15"))
CONTEXT = 2
BATCH = int(os.environ.get("CPLUS_ALIGN_BATCH", "1"))


def cpu_threads() -> int:
    """Threads to give torch: the container's CPU limit, not the host's core count.

    torch sizes itself to every core the kernel reports, which inside a
    container limited to 3 CPUs on an 8-core host means 8 threads contending
    for 3 CPUs' worth of time — measurably slower than 3.
    """
    override = os.environ.get("CPLUS_ALIGN_THREADS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    try:
        available = len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - not Linux
        available = os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    if limit:
        available = min(available, max(1, math.ceil(limit)))
    return max(1, available)


def _cgroup_cpu_limit() -> float | None:
    try:  # cgroup v2
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError):
        return None


class Model:
    """The MMS forced-alignment model, loaded once and kept for the engine's lifetime."""

    def __init__(self, model_dir: Path, threads: int | None = None) -> None:
        import torch
        from transformers import Wav2Vec2ForCTC

        self.threads = threads or cpu_threads()
        torch.set_num_threads(self.threads)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:  # already set in this process
            pass
        self._torch = torch
        self.model = Wav2Vec2ForCTC.from_pretrained(str(model_dir), dtype=torch.float32).eval()
        self.ratio = int(self.model.config.inputs_to_logits_ratio)
        self.stride = self.ratio / SR
        vocab = json.loads((model_dir / "vocab.json").read_text())
        self.vocab = {k.lower(): int(v) for k, v in vocab.items()}
        self.blank = self.vocab.get("<blank>", int(self.model.config.pad_token_id or 0))
        self.size = int(self.model.config.vocab_size)

    def log_probs(self, batch: np.ndarray) -> np.ndarray:
        """``(B, samples)`` float32 audio in [-1, 1] -> ``(B, frames, vocab)`` log-probs."""
        torch = self._torch
        with torch.inference_mode():
            logits = self.model(torch.from_numpy(batch)).logits
            return torch.log_softmax(logits.float(), dim=-1).numpy()


def compute_emissions(
    model: Model,
    raw_path: Path,
    work: Path,
    *,
    progress: Callable[[float], None],
    poll: Callable[[], None],
) -> tuple[np.ndarray, float, float]:
    """Emissions for the whole decoded book -> ``(emissions, stride, duration)``.

    Resumes from ``work/emissions.json`` when it describes the same audio and
    settings; otherwise starts over.
    """
    data = np.memmap(raw_path, dtype=np.int16, mode="r")
    n = len(data)
    ratio = model.ratio
    frames = n // ratio
    W, C = WINDOW * SR, CONTEXT * SR
    per_window = W // ratio
    nwin = math.ceil(n / W)
    em_path, meta_path = work / "emissions.npy", work / "emissions.json"
    shape_key = {"samples": n, "window": WINDOW, "context": CONTEXT, "vocab": model.size}

    meta = None
    if meta_path.exists() and em_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = None
    if meta and all(meta.get(k) == v for k, v in shape_key.items()):
        em = np.lib.format.open_memmap(em_path, mode="r+")
        done = int(meta.get("done", 0))
    else:
        em = np.lib.format.open_memmap(
            em_path, mode="w+", dtype=np.float32, shape=(frames, model.size)
        )
        done = 0

    lead = C // ratio
    for b in range(done, nwin, BATCH):
        chunks = []
        wins = range(b, min(b + BATCH, nwin))
        for w in wins:
            buf = np.zeros(W + 2 * C, dtype=np.float32)
            s, e = w * W - C, w * W + W + C
            lo, hi = max(s, 0), min(e, n)
            if hi > lo:
                buf[lo - s : hi - s] = data[lo:hi] / 32768.0
            chunks.append(buf)
        lp = model.log_probs(np.stack(chunks))
        for k, w in enumerate(wins):
            f0, f1 = w * per_window, min((w + 1) * per_window, frames)
            if f1 > f0:
                em[f0:f1] = lp[k, lead : lead + (f1 - f0)]
        done = wins[-1] + 1
        em.flush()
        write_json(meta_path, {**shape_key, "done": done, "windows": nwin})
        progress(done / nwin)
        poll()

    del em
    em = np.load(em_path, mmap_mode="r")
    return em, model.stride, n / SR
