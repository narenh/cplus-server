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

* ``GET /audiobooks/{ratingKey}/bookmarks``, ``PUT``/``DELETE
  /audiobooks/{ratingKey}/bookmarks/{id}`` — places this user marked in the
  book. The client makes each id, so a retried add is harmless; see
  :class:`~cplus_service.db.models.AudiobookBookmark`.

Progress and bookmarks are kept per Plex Home profile, named by ``X-Canopy-Profile`` (absent
for the account owner); see :func:`~cplus_service.api.deps.get_profile`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...audiobooks.access import AccessUnknown
from ...audiobooks.ingest import decode_chunk
from ...db.models import (
    AudiobookAlignment,
    AudiobookBookmark,
    AudiobookChunk,
    AudiobookProgress,
    Config,
    User,
)
from ...db.session import get_config
from ..deps import CachedUserDep, DbDep, PlexTokenDep, ProfileDep, StateDep
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
        "speed": row.speed,
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This book isn't aligned")
    return alignment


async def _progress_rows(
    db: AsyncSession, user: User, profile: str, server_id: str, rating_keys: list[str]
) -> dict[str, AudiobookProgress]:
    if not rating_keys:
        return {}
    rows = await db.execute(
        select(AudiobookProgress).where(
            AudiobookProgress.user_id == user.id,
            AudiobookProgress.profile == profile,
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
    db: DbDep, state: StateDep, user: CachedUserDep, plex_token: PlexTokenDep, profile: ProfileDep
) -> dict[str, Any]:
    config = await get_config(db)
    try:
        books = await visible_books(db, state, config, plex_token)
    except AccessUnknown as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Couldn't check your access with Plex: {exc}"
        ) from exc
    progress = await _progress_rows(
        db, user, profile, config.plex_server_client_identifier or "", [b.rating_key for b in books]
    )
    return {
        "books": [
            {**_book_json(book), "progress": _progress_json(progress.get(book.rating_key))}
            for book in books
        ]
    }


@router.get("/{rating_key}")
async def book_index(
    rating_key: str,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
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
    progress = await _progress_rows(db, user, profile, alignment.plex_server_id, [rating_key])
    bookmarks = await _bookmark_rows(db, user, profile, alignment.plex_server_id, rating_key)
    return {
        **_book_json(alignment),
        "tracks": alignment.tracks,
        "chapters": [s for s in alignment.sections if s.get("sentences")],
        "chunks": [
            {"n": n, "start": start, "end": end, "first": first, "last": last}
            for n, start, end, first, last in chunks.all()
        ],
        "progress": _progress_json(progress.get(rating_key)),
        "bookmarks": [_bookmark_json(b) for b in bookmarks],
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
    speed: float | None = Field(default=None, ge=0.5, le=3.0, description="Playback rate")


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
    rating_key: str,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
) -> dict[str, Any]:
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    row = await db.get(AudiobookProgress, (user.id, server_id, rating_key, profile))
    return {"progress": _progress_json(row)}


@router.put("/{rating_key}/progress")
async def put_progress(
    rating_key: str,
    body: ProgressIn,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
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

    row = await db.get(AudiobookProgress, (user.id, server_id, rating_key, profile))
    if row is not None and _aware(row.listened_at) >= listened:
        return {"applied": False, "progress": _progress_json(row)}
    if row is None:
        row = AudiobookProgress(
            user_id=user.id, plex_server_id=server_id, rating_key=rating_key, profile=profile
        )
        db.add(row)
    row.position = body.position
    row.track_rating_key = body.track_rating_key
    row.track_offset = body.track_offset
    row.finished = body.finished
    row.device = body.device
    row.speed = body.speed
    row.listened_at = listened
    row.updated_at = now
    await db.flush()
    return {"applied": True, "progress": _progress_json(row)}


# --------------------------------------------------------------------------- #
# Bookmarks
# --------------------------------------------------------------------------- #


class BookmarkIn(BaseModel):
    """A place in the book, and when the listener marked it."""

    position: float = Field(ge=0, description="Seconds from the start of the book, all files")
    created_at: datetime = Field(description="When the listener made the bookmark")
    track_rating_key: str | None = Field(default=None, max_length=32)
    track_offset: float | None = Field(default=None, ge=0)


def _bookmark_json(row: AudiobookBookmark) -> dict[str, Any]:
    return {
        "id": row.id,
        "position": row.position,
        "track_rating_key": row.track_rating_key,
        "track_offset": row.track_offset,
        "created_at": _aware(row.created_at).isoformat(),
    }


async def _bookmark_rows(
    db: AsyncSession, user: User, profile: str, server_id: str, rating_key: str
) -> list[AudiobookBookmark]:
    """The live bookmarks in one book, in book order. Tombstones stay server-side."""
    rows = await db.execute(
        select(AudiobookBookmark)
        .where(
            AudiobookBookmark.user_id == user.id,
            AudiobookBookmark.profile == profile,
            AudiobookBookmark.plex_server_id == server_id,
            AudiobookBookmark.rating_key == rating_key,
            AudiobookBookmark.deleted_at.is_(None),
        )
        .order_by(AudiobookBookmark.position)
    )
    return list(rows.scalars())


async def _bookmark(
    db: AsyncSession, user: User, profile: str, server_id: str, rating_key: str, bookmark_id: UUID
) -> AudiobookBookmark | None:
    row = await db.get(AudiobookBookmark, (user.id, profile, str(bookmark_id)))
    # An id this user already used for another book is not this book's.
    if row is not None and (row.plex_server_id, row.rating_key) != (server_id, rating_key):
        raise HTTPException(status.HTTP_409_CONFLICT, "That bookmark id belongs to another book")
    return row


@router.get("/{rating_key}/bookmarks")
async def list_bookmarks(
    rating_key: str,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
) -> dict[str, Any]:
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    rows = await _bookmark_rows(db, user, profile, server_id, rating_key)
    return {"bookmarks": [_bookmark_json(row) for row in rows]}


@router.put("/{rating_key}/bookmarks/{bookmark_id}")
async def put_bookmark(
    rating_key: str,
    bookmark_id: UUID,
    body: BookmarkIn,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
) -> dict[str, Any]:
    """Add a bookmark, or move one. A deleted bookmark stays deleted.

    ``deleted`` in the answer says the id was already deleted elsewhere, so
    the client should drop it rather than keep retrying.
    """
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    row = await _bookmark(db, user, profile, server_id, rating_key, bookmark_id)
    if row is not None and row.deleted_at is not None:
        return {"deleted": True, "bookmark": None}
    if row is None:
        row = AudiobookBookmark(
            user_id=user.id,
            profile=profile,
            id=str(bookmark_id),
            plex_server_id=server_id,
            rating_key=rating_key,
        )
        db.add(row)
    row.position = body.position
    row.track_rating_key = body.track_rating_key
    row.track_offset = body.track_offset
    row.created_at = _aware(body.created_at)
    await db.flush()
    return {"deleted": False, "bookmark": _bookmark_json(row)}


@router.delete("/{rating_key}/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bookmark(
    rating_key: str,
    bookmark_id: UUID,
    db: DbDep,
    state: StateDep,
    user: CachedUserDep,
    plex_token: PlexTokenDep,
    profile: ProfileDep,
) -> None:
    """Delete a bookmark. Deleting one this server never heard of is not an error."""
    config = await get_config(db)
    server_id = await _check_album(db, state, config, plex_token, rating_key)
    row = await _bookmark(db, user, profile, server_id, rating_key, bookmark_id)
    if row is not None and row.deleted_at is None:
        row.deleted_at = datetime.now(UTC)
