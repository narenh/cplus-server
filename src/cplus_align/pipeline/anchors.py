"""Anchors between audio time and book position, and the segments they imply."""

from __future__ import annotations

import bisect

import numpy as np

from .book import Book, K

MERGE_GAP = 30  # runs closer than this (letters, both sides) are merged
MIN_RUN = 20  # runs shorter than this (letters) are dropped
SEG_SECONDS = 75  # target maximum segment length for forced alignment
UNSPOKEN_Q = 2.5  # book gap > this x the letters the audio gap could hold => unspoken


def letter_lut(vocab: dict[str, int], blank: int, size: int) -> np.ndarray:
    lut = np.zeros(size, dtype=np.uint8)
    for tok, i in vocab.items():
        if len(tok) == 1 and (tok.isalpha() or tok == "'") and i != blank and i < size:
            lut[i] = ord(tok)
    return lut


def greedy_decode(
    em: np.ndarray, vocab: dict[str, int], blank: int, *, block: int = 200_000
) -> tuple[str, np.ndarray]:
    """Collapse the emissions' argmax path into letters, with the frame each came from.

    Works in blocks so a disk-backed emissions array for a 40-hour book is never
    pulled into memory whole.
    """
    lut = letter_lut(vocab, blank, em.shape[1])
    ids = np.empty(len(em), dtype=np.int64)
    for start in range(0, len(em), block):
        ids[start : start + block] = np.asarray(em[start : start + block]).argmax(1)
    prev = np.r_[-1, ids[:-1]]
    keep = (ids != prev) & (lut[ids] > 0)
    frames = np.flatnonzero(keep)
    return bytes(lut[ids[keep]]).decode("ascii"), frames


def seed_hits(decoded: str, seeds: dict[str, int]) -> list[tuple[int, int]]:
    """``(audio letter index, book letter index)`` for every unique seed the audio says."""
    pairs = []
    for i in range(len(decoded) - K + 1):
        j = seeds.get(decoded[i : i + K], -1)
        if j >= 0:
            pairs.append((i, j))
    return pairs


class NoSharedText(RuntimeError):
    """The audio and the epub share no text at all."""


def find_chain(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest chain of seed hits increasing in both audio and book position."""
    if not pairs:
        raise NoSharedText("no shared text found between the audio and the epub")
    tails: list[int] = []
    idx: list[int] = []
    prev = [-1] * len(pairs)
    for n, (_, j) in enumerate(pairs):
        p = bisect.bisect_left(tails, j)
        if p == len(tails):
            tails.append(j)
            idx.append(n)
        else:
            tails[p], idx[p] = j, n
        prev[n] = idx[p - 1] if p else -1
    chain = []
    n = idx[-1]
    while n != -1:
        chain.append(pairs[n])
        n = prev[n]
    return chain[::-1]


def make_runs(chain: list[tuple[int, int]], book: Book) -> list[list[int]]:
    """Merge chained seed hits into runs ``[i0,i1) x [j0,j1)``, snapped to word starts."""
    runs: list[list[int]] = []
    for i, j in chain:
        if runs and i - runs[-1][1] <= MERGE_GAP and j - runs[-1][3] <= MERGE_GAP:
            runs[-1][1], runs[-1][3] = max(runs[-1][1], i + K), max(runs[-1][3], j + K)
        else:
            runs.append([i, i + K, j, j + K])
    nb = len(book.letters)
    wstarts = book.offs[:-1][book.wlen > 0]
    starts = np.r_[wstarts, nb]

    def ceil_b(j: int) -> int:
        return int(starts[np.searchsorted(starts, j, "left")])

    def floor_b(j: int) -> int:
        return int(starts[np.searchsorted(starts, j, "right") - 1])

    out = []
    for i0, i1, j0, j1 in runs:
        nj0, nj1 = ceil_b(j0), floor_b(j1)
        if nj0 - j0 > K or j1 - nj1 > K:
            nj0, nj1 = j0, j1  # very long word: leave unsnapped
        i0, i1 = i0 + (nj0 - j0), i1 - (j1 - nj1)
        if nj1 - nj0 >= MIN_RUN and i1 > i0:
            out.append([i0, i1, nj0, nj1])
    return out


Segment = tuple[int, int, int, int]  # (book0, book1, frame0, frame1)


def plan_segments(
    runs: list[list[int]], frames: np.ndarray, total_frames: int, nb: int, stride: float
) -> tuple[list[Segment], list[tuple[int, int]], list[tuple[float, float]], float]:
    """-> (segments, unspoken book ranges, extra audio ranges, letters per second)."""
    rates = [
        (r[3] - r[2]) / ((frames[r[1] - 1] + 1 - frames[r[0]]) * stride)
        for r in runs
        if r[3] - r[2] >= 100
    ]
    rate = float(np.median(rates)) if rates else 13.0  # book letters per audio second

    def fr0(r: list[int]) -> int:
        return int(frames[r[0]])

    def fr1(r: list[int]) -> int:
        return int(frames[r[1] - 1]) + 1

    def gap_ok(nchars: int, f0: int, f1: int) -> bool:
        return nchars < 30 or nchars <= UNSPOKEN_Q * rate * max((f1 - f0) * stride, 0.05)

    atoms: list[Segment] = []
    dead: list[tuple[int, int]] = []
    extra: list[tuple[float, float]] = []
    if gap_ok(runs[0][2], 0, fr0(runs[0])):
        b0, f0 = 0, 0
    else:
        dead.append((0, runs[0][2]))
        b0, f0 = runs[0][2], fr0(runs[0])
    for k, r in enumerate(runs):
        nxt = runs[k + 1] if k + 1 < len(runs) else None
        gb0, gf0 = r[3], fr1(r)
        gb1, gf1 = (nxt[2], fr0(nxt)) if nxt else (nb, total_frames)
        if gap_ok(gb1 - gb0, gf0, gf1):
            atoms.append((b0, gb1, f0, gf1))
            if gb1 - gb0 < 0.3 * rate * (gf1 - gf0) * stride and (gf1 - gf0) * stride > 15:
                extra.append((gf0 * stride, gf1 * stride))
            b0, f0 = gb1, gf1
        else:
            atoms.append((b0, gb0, f0, gf0))
            dead.append((gb0, gb1))
            b0, f0 = gb1, gf1
        if nxt is None:
            break
    segs: list[Segment] = []
    for a in atoms:
        if (
            segs
            and a[0] == segs[-1][1]
            and a[2] == segs[-1][3]
            and (segs[-1][3] - segs[-1][2]) * stride < SEG_SECONDS
        ):
            segs[-1] = (segs[-1][0], a[1], segs[-1][2], a[3])
        else:
            segs.append(a)
    return segs, dead, extra, rate
