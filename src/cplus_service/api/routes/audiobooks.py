"""Read-along for clients: finished audiobooks, their text a chunk at a time, and progress.

Authenticated like every other client route (``X-Plex-Token``, primed by
``/register``). On top of that, a book is only served to someone who can see
its library in Plex: see :mod:`cplus_service.audiobooks.access`. A book they
cannot see is a 404, the same as one that does not exist.

Only finished alignments are listed. The audio itself is never served here;
a client plays it straight from Plex, and every time in these responses is on
the book's one continuous timeline, with ``tracks[].offset`` saying where each
of its files starts on it.

* ``GET /audiobooks`` — the finished books the caller can see, with their
  progress.
* ``GET /audiobooks/{ratingKey}`` — one book's index: files, chapters, and the
  time range each chunk covers. Enough to seek anywhere by fetching one chunk.
* ``GET /audiobooks/{ratingKey}/chunks/{n}?v={version}`` — about ten minutes
  of sentences. ``version`` comes from the index; a chunk of a given version
  never changes, so it is cacheable forever, and a stale version (the book was
  re-aligned since) answers 409 so the client refetches the index.
* ``GET``/``PUT /audiobooks/{ratingKey}/progress`` — where this user is in the
  book, aligned or not. Newest listen wins; see
  :class:`~cplus_service.db.models.AudiobookProgress`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...audiobooks.access import AccessUnknown
from ...audiobooks.ingest import decode_chunk
from ...db.models import AudiobookAlignment, AudiobookChunk, AudiobookProgress, Config, User
from ...db.session import get_config
from ..deps import CachedUserDep, DbDep, PlexTokenDep, StateDep
from ..state import AppState

router = APIRouter(prefix="/audiobooks", tags=["client"])

#: A device clock this far ahead of ours is wrong, not early; its writes are
#: stamped with our time instead, or it would win every future comparison.
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _progress_json(row: AudiobookProgress | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "position": row.position,
        "track_rating_key": row.track_rating_key,
        "track_offset": row.track_offset,
        "finished": row.finished,
        "listened_at": _aware(row.listened_at).isoformat(),
        "device": row.device,
    }


async def _libraries(state: AppState, config: Config, token: str) -> set[str]:
    try:
        return await state.plex_access.libraries(token, config, state.http)
    except AccessUnknown as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Couldn't check your access with Plex: {exc}"
        ) from exc


async def _visible_alignment(
    db: AsyncSession, state: AppState, config: Config, token: str, rating_key: str
) -> AudiobookAlignment:
    alignment = (
        await db.execute(
            select(AudiobookAlignment).where(
                AudiobookAlignment.plex_server_id == (config.plex_server_client_identifier or ""),
                AudiobookAlignment.rating_key == rating_key,
            )
        )
    ).scalar_one_or_none()
    if alignment is None or alignment.library_id not in await _libraries(state, config, token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No read-along for this book")
    return alignment


async def _progress_rows(
    db: AsyncSession, user: User, server_id: str, rating_keys: list[str]
) -> dict[str, AudiobookProgress]:
    if not rating_keys:
        return {}
    rows = await db.execute(
        select(AudiobookProgress).where(
            AudiobookProgress.user_id == user.id,
            AudiobookProgress.plex_server_id == server_id,
            AudiobookProgress.rating_key.in_(rating_keys),
        )
    )
    return {row.rating_key: row for row in rows.scalars()}


def _book_json(alignment: AudiobookAlignment) -> dict[str, Any]:
    stats = alignment.stats or {}
    return {
        "rating_key": alignment.rating_key,
        "library_id": alignment.library_id,
        "title": alignment.title,
        "author": alignment.author,
        "duration": alignment.duration,
        "version": alignment.id,
        "aligned_at": _aware(alignment.created_at).isoformat(),
        "sentences": stats.get("sentences"),
        "aligned_sentences": stats.get("aligned"),
    }


async def visible_books(
    db: AsyncSession, state: AppState, config: Config, token: str
) -> list[AudiobookAlignment]:
    """Every finished book on this server in a library the caller can see."""
    server_id = config.plex_server_client_identifier
    if not server_id:
        return []
    rows = list(
        (
            await db.execute(
                select(AudiobookAlignment)
                .where(AudiobookAlignment.plex_server_id == server_id)
                .order_by(AudiobookAlignment.title)
            )
        ).scalars()
    )
    if not rows:
        return []
    libraries = await state.plex_access.libraries(token, config, state.http)
    return [row for row in rows if row.library_id in libraries]


@router.get("")
async def list_books(
    db: DbDep, state: StateDep, user: CachedUserDep, plex_token: PlexTokenDep
) -> dict[str, Any]:
    config = await get_config(db)
    try:
        books = await visible_books(db, state, config, plex_token)
    except AccessUnknown as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Couldn't check your access with Plex: {exc}"
        ) from exc
    progress = await _progress_rows(
        db, user, config.plex_server_client_identifier or "", [b.rating_key for b in books]
    )
    return {
        "books": [
            {**_book_json(book), "progress": _progress_json(progress.get(book.rating_key))}
            for book in books
        ]
    }


@router.get("/{rating_key}")
async def book_index(
    rating_key: str, db: DbDep, state: StateDep, user: CachedUserDep, plex_token: PlexTokenDep
) -> dict[str, Any]:
    config = await get_config(db)
    alignment = await _visible_alignment(db, state, config, plex_token, rating_key)
    chunks = await db.execute(
        select(
            AudiobookChunk.n,
            AudiobookChunk.start,
            AudiobookChunk.end,
            AudiobookChunk.first_sentence,
            AudiobookChunk.last_sentence,
        )
        .where(AudiobookChunk.alignment_id == alignment.id)
        .order_by(AudiobookChunk.n)
    )
    progress = await _progress_rows(db, user, alignment.plex_server_id, [rating_key])
    return {
        **_book_json(alignment),
        "tracks": alignment.tracks,
        "chapters": [s for s in alignment.sections if s.get("sentences")],
        "chunks": [
            {"n": n, "start": start, "end": end, "first": first, "last": last}
            for n, start, end, first, last in chunks.all()
        ],
        "progress": _progress_json(progress.get(rating_key)),
    }


@router.get("/{rating_key}/chunks/{n}")
async def book_chunk(
    rating_key: str,
    n: int,
    request: Request,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    v: int = Query(..., description="The book's version, from its index"),
) -> Response:
    config = await get_config(db)
    alignment = await _visible_alignment(db, state, config, plex_token, rating_key)
    if v != alignment.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This book was re-aligned; fetch its index again."
        )
    chunk = await db.get(AudiobookChunk, (alignment.id, n))
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such chunk")
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Vary": "Accept-Encoding",
    }
    # Stored gzipped: pass it straight through to any client that takes gzip.
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(
            chunk.data,
            media_type="application/json",
            headers={**headers, "Content-Encoding": "gzip"},
        )
    return Response(
        _dumps({"sentences": decode_chunk(chunk.data)}),
        media_type="application/json",
        headers=headers,
    )


def _dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


class ProgressIn(BaseModel):
    """Where the listener is, and when they were there."""

    position: float = Field(ge=0, description="Seconds from the start of the book, all files")
    listened_at: datetime = Field(description="When the listener was at this position")
    track_rating_key: str | None = Field(default=None, max_length=32)
    track_offset: float | None = Field(default=None, ge=0)
    finished: bool = False
    device: str | None = Field(default=None, max_length=128)


async def _check_album(
    db: AsyncSession, state: AppState, config: Config, token: str, rating_key: str
) -> str:
    """The server id, once the caller is known to be able to see this album."""
    server_id = config.plex_server_client_identifier
    if not server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Plex server configured")
    alignment = (
        await db.execute(
            select(AudiobookAlignment.library_id).where(
                AudiobookAlignment.plex_server_id == server_id,
                AudiobookAlignment.rating_key == rating_key,
            )
        )
    ).scalar_one_or_none()
    try:
        if alignment is not None:
            allowed = alignment in await state.plex_access.libraries(token, config, state.http)
        else:
            allowed = await state.plex_access.can_see_album(token, config, state.http, rating_key)
    except AccessUnknown as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Couldn't check your access with Plex: {exc}"
        ) from exc
    if not allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such audiobook")
    return server_id


@router.get("/{rating_key}/progress")
async def get_progress(
    rating_key: str, db: DbDep, state: StateDep, user: CachedUserDep, plex_token: PlexTokenDep
) -> dict[str, Any]:
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    row = await db.get(AudiobookProgress, (user.id, server_id, rating_key))
    return {"progress": _progress_json(row)}


@router.put("/{rating_key}/progress")
async def put_progress(
    rating_key: str,
    body: ProgressIn,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
) -> dict[str, Any]:
    """Record a position, unless a newer listen is already on file.

    ``applied`` says whether it was; either way ``progress`` is what is now
    stored, so a device that was behind can jump to where the listener got to
    elsewhere.
    """
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    now = datetime.now(UTC)
    listened = _aware(body.listened_at)
    if listened > now + MAX_CLOCK_SKEW:
        listened = now

    row = await db.get(AudiobookProgress, (user.id, server_id, rating_key))
    if row is not None and _aware(row.listened_at) >= listened:
        return {"applied": False, "progress": _progress_json(row)}
    if row is None:
        row = AudiobookProgress(user_id=user.id, plex_server_id=server_id, rating_key=rating_key)
        db.add(row)
    row.position = body.position
    row.track_rating_key = body.track_rating_key
    row.track_offset = body.track_offset
    row.finished = body.finished
    row.device = body.device
    row.listened_at = listened
    row.updated_at = now
    await db.flush()
    return {"applied": True, "progress": _progress_json(row)}
