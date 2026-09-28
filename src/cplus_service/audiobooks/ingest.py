"""Storing a finished alignment: validated, split into chunks, gzipped.

Clients read a book a chunk at a time (about ten minutes of audio each) rather
than as one document, so opening a 40-hour book costs the same as opening a
short one and seeking is one small request.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AudiobookAlignment, AudiobookChunk, AudiobookJob
from .jobs import fingerprint

#: A chunk closes at the first paragraph break after this much audio.
CHUNK_SECONDS = 600.0
#: ... or after this many sentences, whatever the audio says — a long run the
#: narrator skipped has no times to measure by.
CHUNK_MAX_SENTENCES = 800

#: What each sentence keeps for clients. The scoring fields (``score``,
#: ``min_score``, ``worst``, ``wps``) stay too: they are small and they are what
#: an admin-facing quality view would read.
SENTENCE_FIELDS = ("i", "sec", "para", "text", "start", "end", "flags", "wps", "score")


class InvalidResult(ValueError):
    pass


def load_result(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidResult(f"unreadable result: {exc}") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise InvalidResult("not a version 1 alignment")
    if not isinstance(result.get("sentences"), list) or not result["sentences"]:
        raise InvalidResult("the alignment has no sentences")
    return result


def chunk_sentences(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split into consecutive runs of about :data:`CHUNK_SECONDS`, at paragraph breaks."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    first_time: float | None = None
    for sentence in sentences:
        starts_paragraph = not current or sentence.get("para") != current[-1].get("para")
        start = sentence.get("start")
        if current and starts_paragraph:
            long_enough = (
                first_time is not None and start is not None and start - first_time >= CHUNK_SECONDS
            )
            if long_enough or len(current) >= CHUNK_MAX_SENTENCES:
                chunks.append(current)
                current, first_time = [], None
        current.append(sentence)
        if first_time is None and start is not None:
            first_time = start
    if current:
        chunks.append(current)

    out = []
    for n, run in enumerate(chunks):
        timed = [s for s in run if s.get("start") is not None]
        out.append(
            {
                "n": n,
                "start": min((s["start"] for s in timed), default=None),
                "end": max((s["end"] for s in timed), default=None),
                "first": run[0]["i"],
                "last": run[-1]["i"],
                "sentences": [{k: s.get(k) for k in SENTENCE_FIELDS if k in s} for s in run],
            }
        )
    return out


def encode_chunk(sentences: list[dict[str, Any]]) -> bytes:
    body = json.dumps(sentences, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(body, mtime=0)


def decode_chunk(data: bytes) -> list[dict[str, Any]]:
    return json.loads(gzip.decompress(data))


def build_alignment(job: AudiobookJob, result: dict[str, Any]) -> AudiobookAlignment:
    """The row (with its chunks) for a job's result. Pure: no I/O."""
    audio = result.get("audio") or {}
    offsets = {int(t["n"]): t for t in audio.get("tracks") or [] if "n" in t}
    tracks = []
    for track in job.tracks:
        measured = offsets.get(int(track["n"]), {})
        tracks.append(
            {
                "n": track["n"],
                "rating_key": track.get("rating_key"),
                "part_id": track.get("part_id"),
                "offset": measured.get("offset"),
                "duration": measured.get("duration", track.get("duration")),
            }
        )
    sections = [
        {k: s.get(k) for k in ("index", "title", "start", "end", "sentences")}
        for s in result.get("sections") or []
    ]
    book = result.get("book") or {}
    alignment = AudiobookAlignment(
        plex_server_id=job.plex_server_id,
        rating_key=job.rating_key,
        library_id=job.library_id,
        title=job.title,
        author=job.author,
        duration=float(audio.get("duration") or sum(t["duration"] or 0 for t in job.tracks)),
        tracks=tracks,
        fingerprint=fingerprint(job.tracks),
        sections=sections,
        stats=dict(result.get("stats") or {}),
        epub_title=book.get("title") or job.epub_title,
        epub_author=", ".join(book.get("authors") or []) or job.epub_author,
        job_id=job.id,
    )
    alignment.chunks = [
        AudiobookChunk(
            n=chunk["n"],
            start=chunk["start"],
            end=chunk["end"],
            first_sentence=chunk["first"],
            last_sentence=chunk["last"],
            data=encode_chunk(chunk["sentences"]),
        )
        for chunk in chunk_sentences(result["sentences"])
    ]
    return alignment


async def replace_alignment(db: AsyncSession, alignment: AudiobookAlignment) -> None:
    """Store ``alignment``, replacing any earlier one for the same book."""
    await db.execute(
        delete(AudiobookAlignment).where(
            AudiobookAlignment.plex_server_id == alignment.plex_server_id,
            AudiobookAlignment.rating_key == alignment.rating_key,
        )
    )
    db.add(alignment)
    await db.flush()
