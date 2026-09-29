"""Storing a finished alignment: validated, split into chunks, gzipped.

Clients read a book a chunk at a time (about ten minutes of audio each) rather
than as one document, so opening a 40-hour book costs the same as opening a
short one and seeking is one small request.

Alignments arrive two ways: from the sidecar when a job finishes, and uploaded
by an admin (an alignment already made elsewhere, e.g. with ``bookalign.py`` on
a faster machine). Both are the same ``version: 1`` document and go through the
same checks; an upload is additionally matched against the album's audio in
Plex, since nothing else ties it to that book.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AudiobookAlignment, AudiobookChunk, AudiobookJob
from ..web.copy_strings import text
from .jobs import fingerprint

#: A chunk closes at the first paragraph break after this much audio.
CHUNK_SECONDS = 600.0
#: ... or after this many sentences, whatever the audio says — a long run the
#: narrator skipped has no times to measure by.
CHUNK_MAX_SENTENCES = 800

#: What each sentence keeps for clients. ``wps`` and ``score`` stay too: they are
#: small, and they are what an admin-facing quality view would read.
SENTENCE_FIELDS = ("i", "sec", "para", "text", "start", "end", "flags", "wps", "score")

#: What each chapter keeps for clients. ``title`` is the ready-to-show string; ``part``
#: ("Book Two"), ``number`` and ``name`` (the title without its "Chapter 6:") let a client
#: group and number chapters its own way. Only the fields the aligner wrote are kept, so an
#: older alignment reads exactly as it always did.
SECTION_FIELDS = (
    "index", "title", "start", "end", "sentences", "label", "number", "part", "name",
)  # fmt: skip


class InvalidResult(ValueError):
    pass


#: The largest alignment an admin may upload. The Hobbit's is ~1.5 MB; a
#: 40-hour book's perhaps 8.
MAX_IMPORT_BYTES = 50 * 1024 * 1024


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_result(data: bytes) -> dict[str, Any]:
    """A ``version: 1`` alignment document, checked well enough to store and serve."""
    try:
        result = json.loads(data)
    except ValueError as exc:
        raise InvalidResult(text("py_audiobooks.alignment_not_json.text", error=exc)) from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise InvalidResult(text("py_audiobooks.alignment_not_v1.text"))
    sentences = result.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise InvalidResult(text("py_audiobooks.alignment_no_sentences.text"))
    for index, sentence in enumerate(sentences):
        if not isinstance(sentence, dict) or sentence.get("i") != index:
            raise InvalidResult(text("py_audiobooks.alignment_sentence_order.text", index=index))
        if not isinstance(sentence.get("text"), str):
            raise InvalidResult(text("py_audiobooks.alignment_sentence_no_text.text", index=index))
        if not isinstance(sentence.get("para"), int) or not isinstance(sentence.get("sec"), int):
            raise InvalidResult(text("py_audiobooks.alignment_sentence_no_para.text", index=index))
        start, end = sentence.get("start"), sentence.get("end")
        if (start is None) != (end is None):
            raise InvalidResult(
                text("py_audiobooks.alignment_sentence_half_timed.text", index=index)
            )
        if start is not None and (not _number(start) or not _number(end) or end < start):
            raise InvalidResult(
                text("py_audiobooks.alignment_sentence_bad_times.text", index=index)
            )
    if not isinstance(result.get("sections", []), list):
        raise InvalidResult(text("py_audiobooks.alignment_sections_not_list.text"))
    return result


def load_result(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InvalidResult(text("py_audiobooks.alignment_unreadable.text", error=exc)) from exc
    return parse_result(data)


def _span(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def import_tracks(result: dict[str, Any], tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where each of the album's files starts on the uploaded alignment's timeline.

    Refuses an alignment made from different audio: its duration has to match
    what Plex has for the album (to within half a percent, or five seconds for a
    short book). A many-file album also needs the per-file offsets the aligner
    records; without them there is no telling where one file ends and the next
    begins, and a guess from Plex's durations would drift.
    """
    audio = result.get("audio") or {}
    duration = audio.get("duration")
    if not _number(duration) or duration <= 0:
        raise InvalidResult(text("py_audiobooks.alignment_no_duration.text"))
    plex_total = sum(float(t.get("duration") or 0) for t in tracks)
    if abs(duration - plex_total) > max(5.0, 0.005 * plex_total):
        raise InvalidResult(
            text(
                "py_audiobooks.alignment_wrong_duration.text",
                made=_span(duration),
                plex=_span(plex_total),
            )
        )
    last_end = max((s["end"] for s in result["sentences"] if s.get("end") is not None), default=0)
    if last_end > duration + 2:
        raise InvalidResult(text("py_audiobooks.alignment_past_end.text"))

    given = audio.get("tracks")
    if isinstance(given, list) and len(given) == len(tracks):
        out = []
        for n, track in enumerate(given):
            if not isinstance(track, dict) or not _number(track.get("offset")):
                raise InvalidResult(
                    text("py_audiobooks.alignment_file_no_offset.text", number=n + 1)
                )
            out.append(
                {"n": n, "offset": float(track["offset"]), "duration": track.get("duration")}
            )
        return out
    if len(tracks) == 1:
        return [{"n": 0, "offset": 0.0, "duration": float(duration)}]
    raise InvalidResult(
        text("py_audiobooks.alignment_multi_file_no_offsets.text", count=len(tracks))
    )


def summary(stats: dict[str, Any]) -> str | None:
    """"6,320 of 6,411 sentences aligned.", plus a warning when that is under half."""
    total, aligned = stats.get("sentences"), stats.get("aligned")
    if not total or aligned is None:
        return None
    message = text(
        "py_audiobooks.alignment_summary.text", aligned=f"{aligned:,}", total=f"{total:,}"
    )
    if aligned / total < 0.5:
        message += " " + text("py_audiobooks.alignment_low_match.text")
    return message


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
    sections = [{k: s[k] for k in SECTION_FIELDS if k in s} for s in result.get("sections") or []]
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


def export_result(alignment: AudiobookAlignment, chunks: list[bytes]) -> dict[str, Any]:
    """A stored alignment as a ``version: 1`` document — one an upload accepts back.

    ``chunks`` is each chunk's data, in order. Sentences come back with the
    fields :data:`SENTENCE_FIELDS` kept; the aligner's diagnostics beyond those
    were not stored, so they are not in the file.
    """
    sentences = [sentence for data in chunks for sentence in decode_chunk(data)]
    tracks = alignment.tracks or []
    audio: dict[str, Any] = {"duration": alignment.duration}
    if tracks and all(t.get("offset") is not None for t in tracks):
        audio["tracks"] = [
            {"n": t["n"], "offset": t["offset"], "duration": t.get("duration")} for t in tracks
        ]
    book: dict[str, Any] = {"title": alignment.epub_title or alignment.title}
    if alignment.epub_author:
        book["authors"] = alignment.epub_author.split(", ")
    return {
        "version": 1,
        "book": book,
        "audio": audio,
        "sections": alignment.sections or [],
        "stats": alignment.stats or {},
        "sentences": sentences,
    }
