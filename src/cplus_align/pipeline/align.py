"""CTC forced alignment per segment, and grouping word times into sentences."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .anchors import Segment
from .book import Book

FAST_WPS = 8  # sentences faster than this (words/s, >= 8 words) get flagged "fast"

#: The most memory one segment's alignment may take. The C++ aligner keeps two
#: bits per (target state, frame), so a segment's cost grows with the product of
#: its length in frames and in letters: about 90 MB for ten minutes, about 3 GB
#: for an hour. Only a long stretch with no anchors gets near this; such a
#: segment is skipped and counted as failed rather than allowed to exhaust the
#: container's memory and take the whole job down with it.
MAX_SEGMENT_BYTES = 768 * 1024 * 1024


def segment_bytes(frames: int, targets: int, vocab: int) -> int:
    states = 2 * targets + 1
    return 2 * (states + 1) * max(frames - targets, 0) // 8 + frames * (vocab + 1) * 4


def token_spans(path: np.ndarray, blank: int, ntargets: int) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(path).reshape(-1)
    nonblank = p != blank
    new = nonblank & (p != np.r_[blank, p[:-1]])
    if int(new.sum()) != ntargets:
        raise RuntimeError("alignment path does not cover all targets")
    tok = np.cumsum(new) - 1
    nz = np.flatnonzero(nonblank)
    starts = np.zeros(ntargets, dtype=np.int64)
    ends = np.zeros(ntargets, dtype=np.int64)
    ends[tok[nz]] = nz
    ns = np.flatnonzero(new)
    starts[tok[ns]] = ns
    return starts, ends + 1


@dataclass
class WordTimes:
    start: np.ndarray
    end: np.ndarray
    score: np.ndarray
    failed: int
    longest_segment: float


def align_segments(
    em: np.ndarray,
    segs: list[Segment],
    book: Book,
    vocab: dict[str, int],
    blank: int,
    stride: float,
    *,
    progress: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> WordTimes:
    """Align each segment's letters to its frames. ``em`` excludes the ``<star>`` column."""
    from ctc_forced_aligner import forced_align

    letters, owner = book.letters, book.owner
    nw = len(book.words)
    w_start = np.full(nw, np.nan)
    w_end = np.full(nw, np.nan)
    w_score = np.full(nw, np.nan)
    star = em.shape[1]
    failed = 0
    longest = 0.0
    for n, (b0, b1, f0, f1) in enumerate(segs):
        if progress:
            progress(n / max(len(segs), 1))
        if b1 <= b0 or f1 <= f0:
            continue
        longest = max(longest, (f1 - f0) * stride)
        ids = [vocab[c] for c in letters[b0:b1]]
        targets = np.array([star, *ids, star], dtype=np.int64)
        repeats = int((targets[1:] == targets[:-1]).sum())
        if f1 - f0 < len(targets) + repeats:
            failed += 1  # audio too short for the text
            continue
        if segment_bytes(f1 - f0, len(targets), star) > MAX_SEGMENT_BYTES:
            failed += 1
            if log:
                log(f"segment {n} skipped: {(f1 - f0) * stride:.0f}s is too long to align")
            continue
        block = np.asarray(em[f0:f1], dtype=np.float32)
        lp = np.zeros((1, f1 - f0, star + 1), dtype=np.float32)
        lp[0, :, :star] = block  # <star> stays 0 (log 1): it matches anything
        try:
            path, scores = forced_align(lp, targets[None], blank=blank)
            starts, ends = token_spans(path, blank, len(targets))
        except Exception as exc:  # keep going; the segment stays unaligned
            if log:
                log(f"segment {n} failed: {exc}")
            failed += 1
            continue
        scores = np.asarray(scores).reshape(-1)
        cs = np.r_[0.0, np.cumsum(scores)]
        starts, ends = starts[1:-1], ends[1:-1]  # drop <star>
        wid = owner[b0:b1]
        cut = np.r_[0, np.flatnonzero(np.diff(wid)) + 1]
        ws = starts[cut]
        we = ends[np.r_[cut[1:] - 1, len(wid) - 1]]
        for w, s_, e_ in zip(wid[cut], ws, we, strict=True):
            t0, t1 = (f0 + s_) * stride, (f0 + e_) * stride
            w_start[w] = t0 if np.isnan(w_start[w]) else min(w_start[w], t0)
            w_end[w] = t1 if np.isnan(w_end[w]) else max(w_end[w], t1)
            w_score[w] = (cs[e_] - cs[s_]) / max(e_ - s_, 1)
    if progress:
        progress(1.0)
    return WordTimes(w_start, w_end, w_score, failed, round(longest, 1))


def assemble(book: Book, times: WordTimes) -> list[dict[str, Any]]:
    words, wlen = book.words, book.wlen
    out = []
    for i, s in enumerate(book.sents):
        idx = [w for w in range(s["w0"], s["w1"]) if not np.isnan(times.start[w])]
        spoken_words = int((wlen[s["w0"] : s["w1"]] > 0).sum())
        rec: dict[str, Any] = {
            "i": i,
            "sec": s["sec"],
            "para": s["para"],
            "text": s["text"],
            "start": None,
            "end": None,
            "flags": [],
        }
        if not idx:
            rec["flags"].append("unspoken" if spoken_words else "no_letters")
            out.append(rec)
            continue
        start, end = float(times.start[idx].min()), float(times.end[idx].max())
        sc = times.score[idx]
        k = int(np.argmin(sc))
        wps = len(idx) / max(end - start, 0.01)
        rec.update(
            start=round(start, 2),
            end=round(end, 2),
            wps=round(wps, 1),
            score=round(float(sc.mean()), 2),
            min_score=round(float(sc[k]), 2),
            worst=words[idx[k]],
        )
        if len(idx) >= 8 and wps > FAST_WPS:
            rec["flags"].append("fast")
        if spoken_words and len(idx) < 0.6 * spoken_words:
            rec["flags"].append("partial")
        out.append(rec)
    return out
