"""The Audiobooks tab: read-along for the books in a Plex music library.

The chosen music library's albums, each with the state of its alignment in one
cell: an *Upload alignment* control, or how well the stored one matched.
Alignments are made elsewhere and uploaded finished; cplus only checks, stores
and serves them (see :mod:`cplus_service.audiobooks`).

Every control writes in place and gets back only the cell it changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ....audiobooks.ingest import (
    MAX_IMPORT_BYTES,
    InvalidResult,
    alignments_for,
    build_alignment,
    export_result,
    fingerprint,
    import_tracks,
    parse_result,
    replace_alignment,
    track_payload,
)
from ....db.models import AudiobookAlignment, AudiobookChunk, Config, User
from ....db.session import get_config
from ....plex.client import PlexAlbum, PlexServerClient, PlexServerError
from ....web import templates
from ...deps import DbDep, StateDep
from ...state import AppState
from .deps import AdminPageDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audiobooks", tags=["admin"])

PAGE_SIZE = 50

#: Plex's section type for a music library. Audiobook libraries are music
#: libraries in Plex; there is no separate type to look for.
MUSIC_TYPE = "artist"


# --------------------------------------------------------------------------- #
# Shared context
# --------------------------------------------------------------------------- #


def _plex(config: Config, state: AppState) -> PlexServerClient | None:
    if not config.plex_server_base_url or not config.plex_admin_token:
        return None
    return PlexServerClient(config.plex_server_base_url, config.plex_admin_token, client=state.http)


@dataclass
class BookState:
    """Everything one status cell renders from."""

    rating_key: str
    alignment: AudiobookAlignment | None = None
    #: Why an upload was just refused. Shown above the book's actual state,
    #: never instead of it: a bad file dropped on a finished book leaves the
    #: book finished, and the cell has to keep saying so.
    notice: str | None = None
    stale: bool = False


async def _cell(
    request: Request,
    db: AsyncSession,
    rating_key: str,
    *,
    notice: str | None = None,
) -> Response:
    config = await get_config(db)
    server_id = config.plex_server_client_identifier or ""
    alignment = (await alignments_for(db, server_id, [rating_key])).get(rating_key)
    book = BookState(rating_key, alignment=alignment, notice=notice)
    return templates.TemplateResponse(request, "partials/audiobook_status.html", {"book": book})


async def _stale_keys(plex: PlexServerClient, alignments: list[AudiobookAlignment]) -> set[str]:
    """Books whose audio in Plex is no longer the audio they were aligned to."""
    gate = asyncio.Semaphore(6)

    async def check(alignment: AudiobookAlignment) -> str | None:
        async with gate:
            try:
                parts = await plex.album_parts(alignment.rating_key)
            except PlexServerError:
                return None  # can't tell; say nothing rather than cry wolf
        return (
            alignment.rating_key
            if fingerprint(track_payload(parts)) != alignment.fingerprint
            else None
        )

    found = await asyncio.gather(*(check(a) for a in alignments))
    return {key for key in found if key}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def audiobooks_page(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    library: str | None = Query(default=None),
    q: str = Query(default=""),
    page: int = Query(default=0, ge=0),
) -> Response:
    config = await get_config(db)
    context: dict[str, Any] = {
        "admin": admin,
        "title": "Audiobooks",
        "nav": "audiobooks",
        "libraries": [],
        "library": None,
        "books": [],
        "q": q.strip(),
        "page": page,
        "total": 0,
        "page_size": PAGE_SIZE,
        "plex_error": None,
    }
    plex = _plex(config, state)
    server_id = config.plex_server_client_identifier or ""
    if plex is None or not server_id:
        context["plex_error"] = (
            "Not connected to a Plex server yet. Sign out and back in with Plex."
        )
        return templates.TemplateResponse(request, "audiobooks.html", context)

    try:
        sections = [s for s in await plex.list_library_sections() if s.type == MUSIC_TYPE]
        context["libraries"] = sections
        chosen = next((s for s in sections if s.id == library), sections[0] if sections else None)
        context["library"] = chosen
        if chosen is not None:
            albums, total = await plex.list_albums(
                chosen.id, start=page * PAGE_SIZE, size=PAGE_SIZE, query=context["q"] or None
            )
            context["total"] = total
            keys = [album.rating_key for album in albums]
            done = await alignments_for(db, server_id, keys)
            stale = await _stale_keys(plex, list(done.values()))
            context["books"] = [
                (
                    album,
                    BookState(
                        album.rating_key,
                        alignment=done.get(album.rating_key),
                        stale=album.rating_key in stale,
                    ),
                )
                for album in albums
            ]
    except PlexServerError as exc:
        logger.warning("could not list audiobooks: %s", exc)
        context["plex_error"] = f"Could not reach the Plex server: {exc}"

    return templates.TemplateResponse(request, "audiobooks.html", context)


# --------------------------------------------------------------------------- #
# One book
# --------------------------------------------------------------------------- #


@router.get("/books/{rating_key}/status", response_class=HTMLResponse)
async def book_status(
    request: Request, db: DbDep, admin: AdminPageDep, rating_key: str
) -> Response:
    return await _cell(request, db, rating_key)


async def _read_upload(upload: UploadFile, limit: int) -> bytes | None:
    """The upload's bytes, or ``None`` if it is over ``limit``."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(1 << 20):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/books/{rating_key}/alignment.json")
async def export_alignment(db: DbDep, admin: AdminPageDep, rating_key: str) -> Response:
    """The book's alignment as a file *Upload alignment JSON* takes back."""
    config = await get_config(db)
    server_id = config.plex_server_client_identifier or ""
    alignment = (await alignments_for(db, server_id, [rating_key])).get(rating_key)
    if alignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This book isn't aligned")
    rows = await db.execute(
        select(AudiobookChunk.data)
        .where(AudiobookChunk.alignment_id == alignment.id)
        .order_by(AudiobookChunk.n)
    )
    result = await asyncio.to_thread(export_result, alignment, list(rows.scalars()))
    body = json.dumps(result, ensure_ascii=False, indent=1).encode("utf-8")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", alignment.title).strip() or "audiobook"
    ascii_name = name.encode("ascii", "ignore").decode() or "audiobook"
    disposition = (
        f'attachment; filename="{ascii_name}.alignment.json"; '
        f"filename*=UTF-8''{urllib.parse.quote(name + '.alignment.json')}"
    )
    return Response(
        body, media_type="application/json", headers={"Content-Disposition": disposition}
    )


@router.post("/books/{rating_key}/import", response_class=HTMLResponse)
async def import_alignment(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    rating_key: str,
    alignment: UploadFile,
) -> Response:
    """Store an alignment made elsewhere, replacing any this book already has.

    The upload is checked against the album's audio in Plex (see
    :func:`~cplus_service.audiobooks.ingest.import_tracks`).
    """

    async def reject(message: str) -> Response:
        return await _cell(request, db, rating_key, notice=message)

    config = await get_config(db)
    plex = _plex(config, state)
    server_id = config.plex_server_client_identifier
    if plex is None or not server_id:
        return await reject("Not connected to a Plex server.")

    data = await _read_upload(alignment, MAX_IMPORT_BYTES)
    if data is None:
        return await reject(f"That file is over {MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
    try:
        result = await asyncio.to_thread(parse_result, data)
    except InvalidResult as exc:
        return await reject(f"That isn't an alignment this can use: {exc}.")

    try:
        album = await plex.album(rating_key)
        parts = await plex.album_parts(rating_key) if album else []
    except PlexServerError as exc:
        return await reject(f"Could not reach the Plex server: {exc}")
    if album is None:
        return await reject("Plex no longer has this album.")
    if not parts:
        return await reject("Plex lists no audio files for this album.")
    tracks = track_payload(parts)
    try:
        offsets = import_tracks(result, tracks)
    except InvalidResult as exc:
        return await reject(f"That alignment doesn't fit this book: {exc}")

    result["audio"] = {**(result.get("audio") or {}), "tracks": offsets}
    name = (alignment.filename or "an upload").rsplit("/", 1)[-1][:120]
    stored = await asyncio.to_thread(
        build_alignment, server_id=server_id, album=album, tracks=tracks, result=result
    )
    await replace_alignment(db, stored)
    logger.info("audiobook %s (%s): imported alignment %s", album.title, rating_key, name)
    return await _cell(request, db, rating_key)


@router.post("/books/{rating_key}/delete", response_class=HTMLResponse)
async def delete_alignment(
    request: Request, db: DbDep, admin: AdminPageDep, rating_key: str
) -> Response:
    """Forget a finished alignment. Clients stop offering read-along for the book."""
    config = await get_config(db)
    await db.execute(
        delete(AudiobookAlignment).where(
            AudiobookAlignment.plex_server_id == (config.plex_server_client_identifier or ""),
            AudiobookAlignment.rating_key == rating_key,
        )
    )
    return await _cell(request, db, rating_key)


# --------------------------------------------------------------------------- #
# Cover art
# --------------------------------------------------------------------------- #


@router.get("/cover/{rating_key}")
async def cover(
    db: DbDep, state: StateDep, admin: AdminPageDep, rating_key: str, thumb: str = Query(...)
) -> Response:
    """A small cover, fetched through this service: Plex image URLs need the admin token."""
    if not thumb.startswith("/library/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a Plex library image.")
    config = await get_config(db)
    plex = _plex(config, state)
    if plex is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    path = "/photo/:/transcode?" + str(
        httpx.QueryParams(
            {"width": "96", "height": "96", "minSize": "1", "upscale": "1", "url": thumb}
        )
    )
    try:
        response = await state.http.send(plex.open_stream(path))
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY) from exc
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(
        response.content,
        media_type=response.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=86400"},
    )


__all__ = ["PlexAlbum", "User", "router"]
