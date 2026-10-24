"""Storing a finished alignment: validated, split into chunks, gzipped.

Clients read a book a chunk at a time (about ten minutes of audio each) rather
than as one document, so opening a 40-hour book costs the same as opening a
short one and seeking is one small request.

Alignments are made elsewhere (``bookalign.py``, or the parked aligner in
``parked/cplus_align``) and uploaded by an admin as a ``version: 1`` document.
An upload is checked, then matched against the album's audio in Plex, since
nothing else ties it to that book.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AudiobookAlignment, AudiobookChunk
from ..plex.client import PlexAlbum, PlexAudioPart

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
        raise InvalidResult(f"not JSON ({exc})") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise InvalidResult("not a version 1 alignment")
    sentences = result.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise InvalidResult("the alignment has no sentences")
    for index, sentence in enumerate(sentences):
        if not isinstance(sentence, dict) or sentence.get("i") != index:
            raise InvalidResult(f"sentence {index} is missing or out of order")
        if not isinstance(sentence.get("text"), str):
            raise InvalidResult(f"sentence {index} has no text")
        if not isinstance(sentence.get("para"), int) or not isinstance(sentence.get("sec"), int):
            raise InvalidResult(f"sentence {index} has no paragraph or section")
        start, end = sentence.get("start"), sentence.get("end")
        if (start is None) != (end is None):
            raise InvalidResult(f"sentence {index} has a start or an end, not both")
        if start is not None and (not _number(start) or not _number(end) or end < start):
            raise InvalidResult(f"sentence {index} has impossible times")
    if not isinstance(result.get("sections", []), list):
        raise InvalidResult("sections is not a list")
    return result


def track_payload(parts: list[PlexAudioPart]) -> list[dict[str, Any]]:
    """The album's files as Plex lists them, in listening order."""
    return [
        {
            "n": n,
            "rating_key": part.track_rating_key,
            "title": part.track_title,
            "part_id": part.part_id,
            "key": part.key,
            "size": part.size,
            "duration": part.duration,
            "container": part.container,
        }
        for n, part in enumerate(parts)
    ]


def fingerprint(tracks: list[dict[str, Any]]) -> str:
    """Which files an alignment was made from: part ids and sizes, in order.

    Replacing an album's audio in Plex gives it new parts (or new sizes), so a
    changed fingerprint means the stored timings belong to audio that is gone.
    """
    basis = "|".join(f"{t.get('part_id')}:{t.get('size')}" for t in tracks)
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


async def alignments_for(
    db: AsyncSession, server_id: str, rating_keys: list[str]
) -> dict[str, AudiobookAlignment]:
    if not rating_keys:
        return {}
    rows = await db.execute(
        select(AudiobookAlignment)
        .where(AudiobookAlignment.plex_server_id == server_id)
        .where(AudiobookAlignment.rating_key.in_(rating_keys))
    )
    return {row.rating_key: row for row in rows.scalars()}


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
        raise InvalidResult("the alignment doesn't say how long its audio is")
    plex_total = sum(float(t.get("duration") or 0) for t in tracks)
    if abs(duration - plex_total) > max(5.0, 0.005 * plex_total):
        raise InvalidResult(
            f"it was made from {_span(duration)} of audio, but Plex has {_span(plex_total)}"
            " for this book — is it for a different book or recording?"
        )
    last_end = max((s["end"] for s in result["sentences"] if s.get("end") is not None), default=0)
    if last_end > duration + 2:
        raise InvalidResult("its sentences run past the end of its own audio")

    given = audio.get("tracks")
    if isinstance(given, list) and len(given) == len(tracks):
        out = []
        for n, track in enumerate(given):
            if not isinstance(track, dict) or not _number(track.get("offset")):
                raise InvalidResult(f"file {n + 1} has no offset")
            out.append(
                {"n": n, "offset": float(track["offset"]), "duration": track.get("duration")}
            )
        return out
    if len(tracks) == 1:
        return [{"n": 0, "offset": 0.0, "duration": float(duration)}]
    raise InvalidResult(
        f"Plex has {len(tracks)} files for this book, and the alignment doesn't say where each"
        " one starts. Only an alignment made from all of them, in order, can be used."
    )


def summary(stats: dict[str, Any]) -> str | None:
    """"6,320 of 6,411 sentences aligned.", plus a warning when that is under half."""
    total, aligned = stats.get("sentences"), stats.get("aligned")
    if not total or aligned is None:
        return None
    text = f"{aligned:,} of {total:,} sentences aligned."
    if aligned / total < 0.5:
        text += (
            " Less than half the book was found in the audio — check this is the right"
            " edition."
        )
    return text


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


def build_alignment(
    *, server_id: str, album: PlexAlbum, tracks: list[dict[str, Any]], result: dict[str, Any]
) -> AudiobookAlignment:
    """The row (with its chunks) for an alignment of ``album``. Pure: no I/O."""
    audio = result.get("audio") or {}
    offsets = {int(t["n"]): t for t in audio.get("tracks") or [] if "n" in t}
    stored_tracks = []
    for track in tracks:
        measured = offsets.get(int(track["n"]), {})
        stored_tracks.append(
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
        plex_server_id=server_id,
        rating_key=album.rating_key,
        library_id=album.library_id,
        title=album.title,
        author=album.author,
        duration=float(audio.get("duration") or sum(t["duration"] or 0 for t in tracks)),
        tracks=stored_tracks,
        fingerprint=fingerprint(tracks),
        sections=sections,
        stats=dict(result.get("stats") or {}),
        epub_title=book.get("title"),
        epub_author=", ".join(book.get("authors") or []) or None,
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
