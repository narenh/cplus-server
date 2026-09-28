"""The engine: verifies uploads and aligns books, one job at a time.

The supervisor starts it when there is work and the runtime is installed; it
exits by itself once the queue has been empty for a while, which is what gives
the model's ~2 GB back to the host between books.

Verification never waits behind an alignment. The running job calls
:meth:`Engine.poll` between batches, downloads chunks and segments, and poll
runs any verification that has arrived since, on the model already loaded — so
an admin's upload gets its verdict within about a minute even while a
five-hour book is in progress, without a second copy of the model in memory.
"""

from __future__ import annotations

import os
import shutil
import signal
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from ..epub import EpubError, read_epub
from ..protocol import (
    CANCEL_FILE,
    EPUB_FILE,
    ERROR_FILE,
    JOB_FILE,
    RESULT_FILE,
    STATUS_FILE,
    VERIFY_FILE,
    WORK_DIR,
    AlignPaths,
    align_dir,
    needs_verify,
    read_json,
    ready_to_align,
    write_json,
)
from . import progress as progress_mod
from .align import align_segments, assemble
from .anchors import NoSharedText, find_chain, greedy_decode, make_runs, plan_segments, seed_hits
from .audio import SR, decode, download
from .book import build_book, unique_seeds
from .emissions import CONTEXT, WINDOW, Model, compute_emissions, cpu_threads
from .verify import Track, verify

IDLE_EXIT_SECONDS = 90.0

#: Headroom on top of what a job is computed to need, before it is refused for
#: lack of disk.
DISK_MARGIN = 256 * 1024 * 1024


class Cancelled(Exception):
    pass


class Shutdown(Exception):
    pass


class JobFailed(Exception):
    """A failure worth showing the admin as-is."""


def log(message: str) -> None:
    print(f"[aligner {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Engine:
    def __init__(self, paths: AlignPaths) -> None:
        self.paths = paths
        self._model: Model | None = None
        self.threads = cpu_threads()
        self.current: int | None = None
        self.shutdown = False
        self._last_poll = 0.0
        self._polling = False

    # ------------------------------------------------------------------ plumbing

    def model(self) -> Model:
        if self._model is None:
            log(f"loading model with {self.threads} threads")
            self._model = Model(self.paths.model_dir, self.threads)
        return self._model

    def _mark(self, job_id: int | None, kind: str) -> None:
        write_json(
            self.paths.engine_file,
            {"t": time.time(), "pid": os.getpid(), "job": job_id, "kind": kind},
        )

    def poll(self) -> None:
        """Called from inside long work: honour cancel and shutdown, serve verifications."""
        if self.shutdown:
            raise Shutdown
        now = time.monotonic()
        if self._polling or now - self._last_poll < 1.0:
            return
        self._last_poll = now
        if self.current is not None:
            job = self.paths.job(self.current)
            if (job / CANCEL_FILE).exists() or not job.exists():
                raise Cancelled
        self._polling = True
        try:
            for job_id in self.paths.job_ids():
                if job_id != self.current and needs_verify(self.paths.job(job_id)):
                    self.do_verify(job_id)
        finally:
            self._polling = False
            if self.current is not None:
                self._mark(self.current, "align")

    # ---------------------------------------------------------------- main loop

    def run(self, idle_exit: float = IDLE_EXIT_SECONDS) -> None:
        idle_since = time.monotonic()
        while not self.shutdown:
            if self.step():
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > idle_exit:
                log("queue empty; exiting to free memory")
                return
            else:
                time.sleep(2)

    def step(self) -> bool:
        ids = self.paths.job_ids()
        for job_id in ids:
            if needs_verify(self.paths.job(job_id)):
                self.do_verify(job_id)
                return True
        for job_id in ids:
            if ready_to_align(self.paths.job(job_id)):
                self.do_align(job_id)
                return True
        return False

    # ------------------------------------------------------------ verification

    def do_verify(self, job_id: int) -> None:
        job = self.paths.job(job_id)
        spec = read_json(job / JOB_FILE)
        if spec is None:
            return
        log(f"job {job_id}: verifying")
        self._mark(job_id, "verify")
        started = time.time()

        def report(frac: float, label: str) -> None:
            write_json(
                job / STATUS_FILE,
                {
                    "t": time.time(),
                    "phase": "verifying",
                    "label": label,
                    "pct": round(frac * 100, 1),
                },
            )

        def check_cancel() -> None:
            if self.shutdown:
                raise Shutdown
            if (job / CANCEL_FILE).exists() or not job.exists():
                raise Cancelled

        try:
            try:
                epub = read_epub(job / EPUB_FILE)
            except EpubError as exc:
                raise JobFailed(f"The epub couldn't be read: {exc}") from exc
            tracks = [Track(t["url"], float(t["duration"])) for t in spec["tracks"]]
            verdict = verify(
                epub, tracks, spec.get("headers", {}), self.model,
                progress=report, poll=check_cancel,
            )  # fmt: skip
        except Cancelled:
            write_json(job / ERROR_FILE, {"t": time.time(), "code": "cancelled"})
            return
        except JobFailed as exc:
            verdict = {"ok": False, "message": str(exc)}
        except Shutdown:
            raise
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            verdict = {"ok": False, "message": f"Verification failed: {exc}"}
        verdict["t"] = time.time()
        verdict["seconds"] = round(time.time() - started, 1)
        write_json(job / VERIFY_FILE, verdict)
        write_json(
            job / STATUS_FILE,
            {"t": time.time(), "phase": "queued" if verdict["ok"] else "failed", "pct": 0},
        )
        log(f"job {job_id}: verified ok={verdict['ok']} ({verdict.get('message', '')})")

    # --------------------------------------------------------------- alignment

    def do_align(self, job_id: int) -> None:
        job = self.paths.job(job_id)
        spec = read_json(job / JOB_FILE)
        if spec is None:
            return
        self.current = job_id
        self._mark(job_id, "align")
        log(f"job {job_id}: aligning")
        work = job / WORK_DIR
        try:
            result = self._align(job, spec, work)
            write_json(job / RESULT_FILE, result)
            write_json(job / STATUS_FILE, {"t": time.time(), "phase": "done", "pct": 100})
            log(f"job {job_id}: done — {result['stats']['aligned']}/"
                f"{result['stats']['sentences']} sentences aligned")  # fmt: skip
            shutil.rmtree(work, ignore_errors=True)
        except Cancelled:
            log(f"job {job_id}: cancelled")
            if job.exists():
                write_json(job / ERROR_FILE, {"t": time.time(), "code": "cancelled"})
            shutil.rmtree(work, ignore_errors=True)
        except Shutdown:
            log(f"job {job_id}: interrupted; will resume")
            raise
        except JobFailed as exc:
            self._fail(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            self._fail(job, f"Alignment failed: {exc}")
        finally:
            self.current = None
            self._mark(None, "idle")

    def _fail(self, job: Path, message: str) -> None:
        log(f"{job.name}: failed — {message}")
        if job.exists():
            write_json(job / ERROR_FILE, {"t": time.time(), "code": "failed", "message": message})
            write_json(job / STATUS_FILE, {"t": time.time(), "phase": "failed", "pct": 0})
        shutil.rmtree(job / WORK_DIR, ignore_errors=True)

    def _align(self, job: Path, spec: dict[str, Any], work: Path) -> dict[str, Any]:
        tracks = spec["tracks"]
        headers = spec.get("headers", {})
        duration = float(spec.get("duration") or sum(float(t["duration"]) for t in tracks))
        size = sum(int(t.get("size") or 0) for t in tracks)
        work.mkdir(parents=True, exist_ok=True)
        audio_dir = work / "audio"
        raw = work / "audio.s16"
        decode_meta = work / "decode.json"

        calibration = progress_mod.load_calibration(self.paths.calibration_file)
        predicted = progress_mod.predicted_seconds(
            calibration, duration=duration, size=size, threads=self.threads
        )
        tracker = progress_mod.JobProgress(job / STATUS_FILE, predicted)
        resumed: set[str] = set()

        emissions_meta = read_json(work / "emissions.json") or {}
        emissions_complete = (
            emissions_meta.get("windows")
            and emissions_meta.get("done") == emissions_meta.get("windows")
            and (work / "emissions.npy").exists()
            and read_json(decode_meta) is not None
        )

        if not emissions_complete:
            self._check_disk(work, audio_dir, tracks, duration)

            # ---- download
            files = []
            done_bytes = 0
            pending = []
            for n, track in enumerate(tracks):
                ext = (track.get("container") or "audio").lower()
                dest = audio_dir / f"{n:03d}.{ext}"
                files.append(dest)
                if dest.exists():
                    done_bytes += dest.stat().st_size
                else:
                    pending.append((n, track, dest))
            if not pending:
                tracker.skip("download")
                resumed.add("download")
            else:
                audio_dir.mkdir(parents=True, exist_ok=True)
                tracker.start("download", frac=done_bytes / size if size else 0.0)
                for n, track, dest in pending:
                    base = done_bytes
                    label = f"track {n + 1} of {len(tracks)}" if len(tracks) > 1 else ""
                    download(
                        track["url"], headers, dest,
                        expected_size=int(track["size"]) if track.get("size") else None,
                        progress=lambda have, base=base, label=label: tracker.update(
                            (base + have) / size if size else 0.0, label
                        ),
                        poll=self.poll,
                    )  # fmt: skip
                    done_bytes += dest.stat().st_size
                tracker.finish_stage()

            # ---- decode
            meta = read_json(decode_meta)
            if meta and raw.exists() and raw.stat().st_size == 2 * sum(meta["samples"]):
                tracker.skip("decode")
                resumed.add("decode")
            else:
                expected = max(duration * SR * 2, 1)
                tracker.start("decode")
                counts = decode(
                    files, raw,
                    progress=lambda written: tracker.update(min(written / expected, 0.99)),
                    poll=self.poll,
                )  # fmt: skip
                if sum(counts) < SR:
                    raise JobFailed("The audio decoded to nothing.")
                write_json(decode_meta, {"samples": counts})
                tracker.finish_stage()

            # ---- emissions
            model = self.model()
            n_samples = raw.stat().st_size // 2
            start = _resume_fraction(work, n_samples, model.size)
            if start > 0:
                resumed.add("emissions")
            tracker.start("emissions", frac=start)
            em, stride, _ = compute_emissions(
                model, raw, work, progress=tracker.update, poll=self.poll
            )
            tracker.finish_stage()
            # The audio is no longer needed; the emissions carry everything left to do.
            raw.unlink(missing_ok=True)
            shutil.rmtree(audio_dir, ignore_errors=True)
        else:
            for stage in ("download", "decode", "emissions"):
                tracker.skip(stage)
                resumed.add(stage)
            model = self.model()
            em = np.load(work / "emissions.npy", mmap_mode="r")
            stride = model.stride

        counts = read_json(decode_meta)["samples"]  # type: ignore[index]
        total_seconds = sum(counts) / SR

        # ---- anchors
        tracker.start("anchors")
        try:
            epub = read_epub(job / EPUB_FILE)
        except EpubError as exc:
            raise JobFailed(f"The epub couldn't be read: {exc}") from exc
        book = build_book(epub)
        if not book.sents:
            raise JobFailed("The epub has no sentences to align.")
        model = self.model()
        decoded, frames = greedy_decode(em, model.vocab, model.blank)
        self.poll()
        pairs = seed_hits(decoded, unique_seeds(book.letters))
        try:
            chain = find_chain(pairs)
        except NoSharedText as exc:
            raise JobFailed("The audio and the epub share no text.") from exc
        runs = make_runs(chain, book)
        if not runs:
            raise JobFailed("No reliable anchors between the audio and the epub.")
        segs, dead, extra, rate = plan_segments(runs, frames, len(em), len(book.letters), stride)
        log(
            f"{job.name}: {len(pairs)} seed hits -> {len(chain)} chained -> {len(runs)} runs -> "
            f"{len(segs)} segments, {len(dead)} unspoken ranges, {rate:.1f} letters/s"
        )
        tracker.finish_stage()

        # ---- align
        tracker.start("align")

        def on_segment(frac: float) -> None:
            tracker.update(frac)
            self.poll()

        times = align_segments(
            em, segs, book, model.vocab, model.blank, stride, progress=on_segment, log=log
        )
        tracker.finish_stage()

        # ---- assemble
        tracker.start("save")
        sents = assemble(book, times)
        for sec in book.sections:
            timed = [s for s in sents if s["sec"] == sec["index"] and s["start"] is not None]
            sec["start"] = min((s["start"] for s in timed), default=None)
            sec["end"] = max((s["end"] for s in timed), default=None)
            sec["sentences"] = sum(1 for s in sents if s["sec"] == sec["index"])
        aligned = sum(s["start"] is not None for s in sents)

        offsets, acc = [], 0
        for count in counts:
            offsets.append(round(acc / SR, 3))
            acc += count
        track_meta = [
            {
                "n": n,
                "rating_key": track.get("rating_key"),
                "offset": offsets[n],
                "duration": round(counts[n] / SR, 3),
            }
            for n, track in enumerate(tracks)
        ]
        tracker.finish_stage()
        try:
            progress_mod.record_calibration(
                self.paths.calibration_file, tracker,
                duration=total_seconds, size=size, threads=self.threads, resumed=resumed,
            )  # fmt: skip
        except OSError:
            pass
        return {
            "version": 1,
            "book": {"title": epub.title, "authors": epub.authors},
            "audio": {"duration": round(total_seconds, 2), "tracks": track_meta},
            "sections": book.sections,
            "sentences": sents,
            "extra_audio": [[round(a, 1), round(b, 1)] for a, b in extra],
            "stats": {
                "sentences": len(sents),
                "aligned": aligned,
                "unspoken": len(sents) - aligned,
                "fast": sum("fast" in s["flags"] for s in sents),
                "failed_segments": times.failed,
                "segments": len(segs),
                "longest_segment": times.longest_segment,
                "letters_per_sec": round(rate, 2),
                "threads": self.threads,
                "window": WINDOW,
                "seconds": {k: round(v) for k, v in tracker.actual.items()},
            },
        }

    def _check_disk(self, work: Path, audio_dir: Path, tracks: list[dict], duration: float) -> None:
        have = sum(p.stat().st_size for p in audio_dir.glob("*")) if audio_dir.exists() else 0
        audio = sum(int(t.get("size") or 0) for t in tracks)
        need = max(audio - have, 0) + int(duration * SR * 2) + int(duration * 50 * 32 * 4)
        free = shutil.disk_usage(work).free
        if free < need + DISK_MARGIN:
            raise JobFailed(
                "Not enough disk space in the aligner volume: this book needs about "
                f"{(need + DISK_MARGIN) / 1e9:.1f} GB while it runs and "
                f"{free / 1e9:.1f} GB is free."
            )


def _resume_fraction(work: Path, n_samples: int, vocab: int) -> float:
    meta = read_json(work / "emissions.json")
    if not meta or not (work / "emissions.npy").exists():
        return 0.0
    if (
        meta.get("samples") != n_samples
        or meta.get("window") != WINDOW
        or meta.get("context") != CONTEXT
        or meta.get("vocab") != vocab
    ):
        return 0.0
    windows = meta.get("windows") or 0
    return (meta.get("done") or 0) / windows if windows else 0.0


def main() -> None:
    root = align_dir()
    if root is None:
        raise SystemExit("CPLUS_ALIGN_DIR is not set")
    engine = Engine(AlignPaths(root))

    def stop(signum: int, _frame: Any) -> None:
        engine.shutdown = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        engine.run()
    except Shutdown:
        log("stopping")
