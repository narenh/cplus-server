"""Does this epub match this audio? Answered in about a minute, before hours are spent.

Short samples spread across the book (10% to 90%, which skips intros and
credits) are read from Plex by seeking, run through the model, and
greedy-decoded to letters. Each sample is then checked for 12-letter sequences
that occur exactly once in the epub. Narration of the book hits dozens of them
in a few seconds of speech; any other book hits next to none, because a
12-letter run unique to one text is very unlikely to turn up in another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..epub import EpubBook
from .anchors import greedy_decode, seed_hits
from .audio import SR, read_sample
from .book import letters_only, unique_seeds

SAMPLES = 8
SAMPLE_SECONDS = 20.0

#: A sample "matches" at this many unique-seed hits. Calibrated on public-domain
#: LibriVox recordings against their Gutenberg texts: 20 s of matching narration
#: scored 15-135 (one sample, at a pause between stories, 2); the same audio
#: against a different book scored 0-4.
MATCH_HITS = 8

#: The verdict. Pass when at least this share of the readable samples match;
#: between this and :data:`WARN_FRACTION` passes with a warning (an abridged
#: recording, a different edition, long music or credits).
PASS_FRACTION = 0.25
WARN_FRACTION = 0.6


@dataclass
class Track:
    url: str
    duration: float


def sample_points(tracks: list[Track], count: int = SAMPLES) -> list[tuple[int, float]]:
    """``(track index, offset in track)`` for ``count`` points from 10% to 90% of the book."""
    total = sum(t.duration for t in tracks)
    if total <= 0 or not tracks:
        return []
    points = []
    for k in range(count):
        target = total * (0.1 + 0.8 * k / max(count - 1, 1))
        acc = 0.0
        for i, track in enumerate(tracks):
            if target < acc + track.duration or i == len(tracks) - 1:
                offset = min(max(target - acc, 0.0), max(track.duration - SAMPLE_SECONDS, 0.0))
                points.append((i, offset))
                break
            acc += track.duration
    return points


def verify(
    epub: EpubBook,
    tracks: list[Track],
    headers: dict[str, str],
    model_provider: Callable[[], Any],
    *,
    progress: Callable[[float, str], None],
    poll: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Run the check. Returns the ``verify.json`` payload."""
    seeds = unique_seeds(letters_only(epub))
    points = sample_points(tracks)
    if not points:
        return {"ok": False, "message": "Plex reports no audio for this book."}

    steps = len(points) * 2 + 1
    step = 0
    progress(0.0, "Reading audio samples")
    samples: list[np.ndarray | None] = []
    errors: list[str] = []
    for index, offset in points:
        try:
            audio = read_sample(tracks[index].url, headers, offset, SAMPLE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - reported as a verdict, not raised
            errors.append(str(exc))
            audio = None
        samples.append(audio if audio is not None and len(audio) >= 5 * SR else None)
        step += 1
        progress(step / steps, "Reading audio samples")
        poll()

    readable = [s for s in samples if s is not None]
    if not readable:
        detail = f" ({errors[-1]})" if errors else ""
        return {"ok": False, "message": f"Couldn't read the audio from Plex{detail}."}

    progress(step / steps, "Loading the model")
    model = model_provider()
    step += 1

    hits: list[int | None] = []
    for audio in samples:
        if audio is None:
            hits.append(None)
        else:
            lp = model.log_probs((audio.astype(np.float32) / 32768.0)[None])[0]
            decoded, _ = greedy_decode(lp, model.vocab, model.blank)
            hits.append(len(seed_hits(decoded, seeds)))
        step += 1
        progress(step / steps, "Checking the samples against the book")
        poll()

    scored = [h for h in hits if h is not None]
    matched = sum(1 for h in scored if h >= MATCH_HITS)
    share = matched / len(scored)
    result: dict[str, Any] = {
        "ok": share >= PASS_FRACTION,
        "matched": matched,
        "samples": len(scored),
        "hits": hits,
    }
    if share < PASS_FRACTION:
        result["message"] = (
            f"This epub doesn't match the audio: {matched} of {len(scored)} samples "
            "of narration were found in the text."
        )
    elif share < WARN_FRACTION:
        result["warning"] = (
            f"Only {matched} of {len(scored)} samples matched the text — an abridged "
            "recording or a different edition? Aligning anyway; stretches that don't "
            "match will be left unaligned."
        )
        result["message"] = result["warning"]
    else:
        result["message"] = f"{matched} of {len(scored)} samples matched the text."
    return result
