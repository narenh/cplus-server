"""The Audiobooks tab: read-along for the books in a Plex music library.

One card for the aligner itself — off until an admin enables it, because
enabling downloads ~1.5 GB — and below it the chosen music library's albums,
each with the state of its alignment in one cell: an upload control, a
"Verifying…" or "Processing…" bar, or a check mark.

Every control writes in place and gets back only the cell it changed. A cell
with a job in flight re-requests itself on a timer (every 2 s while verifying,
every 10 s while processing) and stops once there is nothing left to watch.

The work happens in the ``cplus-aligner`` sidecar; see
:mod:`cplus_service.audiobooks` for how the two sides talk.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_align.epub import inspect_epub
from cplus_align.protocol import STATUS_STALE

from ....audiobooks import runtime as aligner
from ....audiobooks.ingest import (
    MAX_IMPORT_BYTES,
    InvalidResult,
    build_alignment,
    export_result,
    import_tracks,
    parse_result,
    replace_alignment,
    summary,
)
from ....audiobooks.jobs import (
    MAX_EPUB_BYTES,
    alignments_for,
    cancel_job,
    fingerprint,
    latest_jobs,
    queue_position,
    start_job,
    track_payload,
)
from ....db.models import (
    AudiobookAlignment,
    AudiobookChunk,
    AudiobookJob,
    AudiobookJobStatus,
    Config,
    User,
)
from ....db.session import get_config
from ....plex.client import PlexAlbum, PlexServerClient, PlexServerError
from ....web import format_bytes, templates
from ....web.copy_strings import text
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
    kind: str  # none | verifying | queued | running | ready | failed
    job: AudiobookJob | None = None
    alignment: AudiobookAlignment | None = None
    message: str | None = None
    #: Why an upload was just refused. Shown above the book's actual state,
    #: never instead of it: a bad file dropped on a finished book leaves the
    #: book finished, and the cell has to keep saying so.
    notice: str | None = None
    position: int | None = None
    stale: bool = False
    stalled_minutes: int | None = None
    #: Hours of audio per hour of work, over the whole job as now estimated:
    #: the book's length over time so far plus time left.
    speed: float | None = None
    can_align: bool = False

    @property
    def polling(self) -> str | None:
        """The ``hx-trigger`` that keeps a cell with a job in flight current."""
        if self.kind == "verifying":
            return "every 2s"
        if self.kind in ("queued", "running"):
            return "every 10s"
        return None


def _speed(job: AudiobookJob) -> float | None:
    if job.started_at is None or job.eta_seconds is None:
        return None
    started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=UTC)
    total = (datetime.now(UTC) - started).total_seconds() + job.eta_seconds
    audio = sum(float(t.get("duration") or 0) for t in job.tracks or [])
    return audio / total if total > 0 and audio > 0 else None


async def _book_state(
    db: AsyncSession,
    rating_key: str,
    *,
    server_id: str,
    runtime_ready: bool,
    job: AudiobookJob | None = None,
    alignment: AudiobookAlignment | None = None,
    lookup: bool = True,
    stale: bool = False,
    notice: str | None = None,
) -> BookState:
    if lookup:
        job = (await latest_jobs(db, server_id, [rating_key])).get(rating_key)
        alignment = (await alignments_for(db, server_id, [rating_key])).get(rating_key)
    state = BookState(
        rating_key,
        "none",
        job=job,
        alignment=alignment,
        stale=stale,
        can_align=runtime_ready,
        notice=notice,
    )
    if job is not None and job.is_active:
        state.kind = AudiobookJobStatus(job.status).value
        if job.status in (AudiobookJobStatus.QUEUED, AudiobookJobStatus.RUNNING):
            state.position = await queue_position(db, job)
        if job.status == AudiobookJobStatus.RUNNING and job.heard_at is not None:
            heard = job.heard_at if job.heard_at.tzinfo else job.heard_at.replace(tzinfo=UTC)
            silent = (datetime.now(UTC) - heard).total_seconds()
            if silent > STATUS_STALE:
                state.stalled_minutes = int(silent // 60)
        if job.status == AudiobookJobStatus.RUNNING:
            state.speed = _speed(job)
        return state
    if alignment is not None:
        state.kind = "ready"
        # A replacement that failed or was cancelled leaves the old alignment
        # in place; say what happened to the new one.
        if (
            job is not None
            and job.status == AudiobookJobStatus.FAILED
            and job.id != alignment.job_id
        ):
            state.message = job.message
        return state
    if job is not None and job.status == AudiobookJobStatus.FAILED:
        state.kind, state.message = "failed", job.message
    return state


async def _cell(request: Request, db: AsyncSession, rating_key: str, **kwargs: Any) -> Response:
    config = await get_config(db)
    runtime = aligner.read_runtime()
    book = await _book_state(
        db,
        rating_key,
        server_id=config.plex_server_client_identifier or "",
        runtime_ready=runtime.ready,
        **kwargs,
    )
    return templates.TemplateResponse(request, "partials/audiobook_status.html", {"book": book})


def _runtime_card(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "partials/audiobook_runtime.html", {"runtime": aligner.read_runtime()}
    )


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
    runtime = aligner.read_runtime()
    context: dict[str, Any] = {
        "admin": admin,
        "title": "Audiobooks",
        "nav": "audiobooks",
        "runtime": runtime,
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
        context["plex_error"] = text("py_admin.plex_not_connected_signin.text")
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
            jobs = await latest_jobs(db, server_id, keys)
            done = await alignments_for(db, server_id, keys)
            stale = await _stale_keys(plex, list(done.values()))
            context["books"] = [
                (
                    album,
                    await _book_state(
                        db,
                        album.rating_key,
                        server_id=server_id,
                        runtime_ready=runtime.ready,
                        job=jobs.get(album.rating_key),
                        alignment=done.get(album.rating_key),
                        lookup=False,
                        stale=album.rating_key in stale,
                    ),
                )
                for album in albums
            ]
    except PlexServerError as exc:
        logger.warning("could not list audiobooks: %s", exc)
        context["plex_error"] = text("py_admin.plex_unreachable.text", error=exc)

    return templates.TemplateResponse(request, "audiobooks.html", context)


# --------------------------------------------------------------------------- #
# The aligner runtime
# --------------------------------------------------------------------------- #


@router.get("/runtime", response_class=HTMLResponse)
async def runtime_card(
    request: Request, admin: AdminPageDep, was: str | None = Query(default=None)
) -> Response:
    """The card alone — polled while an install or removal runs.

    When the state it was polled from has ended, the whole page reloads: every
    book's cell depends on whether aligning is possible now.
    """
    response = _runtime_card(request)
    if was and aligner.read_runtime().status != was:
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/runtime/enable", response_class=HTMLResponse)
async def enable_runtime(request: Request, admin: AdminPageDep) -> Response:
    """Install, update or retry — all the same request: fetch whatever is missing."""
    paths = aligner.paths()
    runtime = aligner.read_runtime(paths)
    if paths is None or runtime.status in ("unconfigured", "offline"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The aligner service isn't running.")
    if runtime.status in ("installing", "removing"):
        return _runtime_card(request)
    if not runtime.installed and not runtime.enough_space:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Canopy+ Audiobooks requires {runtime.min_free} of disk space "
            f"({format_bytes(runtime.free_bytes)} available).",
        )
    aligner.request(paths, "install")
    return _runtime_card(request)


@router.post("/runtime/disable", response_class=HTMLResponse)
async def disable_runtime(request: Request, db: DbDep, admin: AdminPageDep) -> Response:
    """Remove the downloaded runtime. Finished alignments are not touched."""
    paths = aligner.paths()
    runtime = aligner.read_runtime(paths)
    if paths is None or runtime.status in ("unconfigured", "offline"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The aligner service isn't running.")
    rows = await db.execute(
        select(AudiobookJob).where(
            AudiobookJob.status.in_(
                [
                    AudiobookJobStatus.VERIFYING,
                    AudiobookJobStatus.QUEUED,
                    AudiobookJobStatus.RUNNING,
                ]
            )
        )
    )
    for job in rows.scalars():
        cancel_job(paths, job)
    aligner.request(paths, "uninstall")
    return _runtime_card(request)


# --------------------------------------------------------------------------- #
# One book
# --------------------------------------------------------------------------- #


@router.get("/books/{rating_key}/status", response_class=HTMLResponse)
async def book_status(
    request: Request, db: DbDep, admin: AdminPageDep, rating_key: str
) -> Response:
    return await _cell(request, db, rating_key)


async def _read_upload(upload: UploadFile, limit: int = MAX_EPUB_BYTES) -> bytes | None:
    """The upload's bytes, or ``None`` if it is over ``limit``."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(1 << 20):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/books/{rating_key}/align", response_class=HTMLResponse)
async def align_book(
    request: Request,
    db: DbDep,
    state: StateDep,
    admin: AdminPageDep,
    rating_key: str,
    epub: UploadFile,
) -> Response:
    """Take an epub for this book: check it, then hand it to the aligner."""

    async def reject(message: str) -> Response:
        return await _cell(request, db, rating_key, notice=message)

    config = await get_config(db)
    paths = aligner.paths()
    runtime = aligner.read_runtime(paths)
    if paths is None or not runtime.ready:
        return await reject(text("py_admin.audiobook_read_along_off.text"))
    plex = _plex(config, state)
    server_id = config.plex_server_client_identifier
    if plex is None or not server_id:
        return await reject(text("py_admin.plex_not_connected.text"))

    active = (await latest_jobs(db, server_id, [rating_key])).get(rating_key)
    if active is not None and active.is_active:
        return await reject(text("py_admin.audiobook_already_aligning_epub.text"))

    data = await _read_upload(epub)
    if data is None:
        return await reject(
            text("py_admin.audiobook_file_too_big.text", mb=MAX_EPUB_BYTES // (1024 * 1024))
        )
    info = await asyncio.to_thread(inspect_epub, io.BytesIO(data))
    if not info.ok:
        return await reject(" ".join(info.problems))

    try:
        album = await plex.album(rating_key)
        parts = await plex.album_parts(rating_key) if album else []
    except PlexServerError as exc:
        return await reject(text("py_admin.plex_unreachable.text", error=exc))
    if album is None:
        return await reject(text("py_admin.audiobook_album_gone.text"))
    if not parts:
        return await reject(text("py_admin.audiobook_no_audio_files.text"))

    job = await start_job(
        db, paths, server_id=server_id, album=album, parts=parts, epub=data, info=info, admin=admin
    )
    logger.info(
        "audiobook %s (%s): queued job %s for verification", album.title, rating_key, job.id
    )
    return await _cell(request, db, rating_key)


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
    """Store an alignment made elsewhere — ``bookalign.py`` on a faster machine, say.

    Needs nothing from the aligner: nothing is computed, so it works with
    read-along switched off. The upload is checked against the album's audio in
    Plex (see :func:`~cplus_service.audiobooks.ingest.import_tracks`), and
    recorded as a job that finished the moment it started, so the book's
    history says where its alignment came from.
    """

    async def reject(message: str) -> Response:
        return await _cell(request, db, rating_key, notice=message)

    config = await get_config(db)
    plex = _plex(config, state)
    server_id = config.plex_server_client_identifier
    if plex is None or not server_id:
        return await reject(text("py_admin.plex_not_connected.text"))
    active = (await latest_jobs(db, server_id, [rating_key])).get(rating_key)
    if active is not None and active.is_active:
        return await reject(text("py_admin.audiobook_being_aligned.text"))

    data = await _read_upload(alignment, MAX_IMPORT_BYTES)
    if data is None:
        return await reject(
            text("py_admin.audiobook_file_too_big.text", mb=MAX_IMPORT_BYTES // (1024 * 1024))
        )
    try:
        result = await asyncio.to_thread(parse_result, data)
    except InvalidResult as exc:
        return await reject(text("py_admin.audiobook_import_unusable.text", error=exc))

    try:
        album = await plex.album(rating_key)
        parts = await plex.album_parts(rating_key) if album else []
    except PlexServerError as exc:
        return await reject(text("py_admin.plex_unreachable.text", error=exc))
    if album is None:
        return await reject(text("py_admin.audiobook_album_gone.text"))
    if not parts:
        return await reject(text("py_admin.audiobook_no_audio_files.text"))
    tracks = track_payload(parts)
    try:
        offsets = import_tracks(result, tracks)
    except InvalidResult as exc:
        return await reject(text("py_admin.audiobook_import_mismatch.text", error=exc))

    result["audio"] = {**(result.get("audio") or {}), "tracks": offsets}
    book = result.get("book") or {}
    now = datetime.now(UTC)
    name = (alignment.filename or "an upload").rsplit("/", 1)[-1][:120]
    job = AudiobookJob(
        plex_server_id=server_id,
        rating_key=album.rating_key,
        library_id=album.library_id,
        title=album.title,
        author=album.author,
        status=AudiobookJobStatus.DONE,
        progress=100.0,
        secret=secrets.token_urlsafe(32),
        tracks=tracks,
        epub_title=book.get("title"),
        epub_author=", ".join(book.get("authors") or []) or None,
        created_by=admin.id,
        started_at=now,
        finished_at=now,
    )
    db.add(job)
    await db.flush()
    stored = await asyncio.to_thread(build_alignment, job, result)
    await replace_alignment(db, stored)
    job.message = " ".join(
        part
        for part in (
            text("py_admin.audiobook_imported_from.text", name=name),
            summary(stored.stats or {}),
        )
        if part
    )
    logger.info("audiobook %s (%s): imported alignment %s", album.title, rating_key, name)
    return await _cell(request, db, rating_key)


@router.post("/books/{rating_key}/cancel", response_class=HTMLResponse)
async def cancel_book(
    request: Request, db: DbDep, admin: AdminPageDep, rating_key: str
) -> Response:
    config = await get_config(db)
    job = (await latest_jobs(db, config.plex_server_client_identifier or "", [rating_key])).get(
        rating_key
    )
    if job is not None and job.is_active:
        cancel_job(aligner.paths(), job)
        await db.flush()
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
