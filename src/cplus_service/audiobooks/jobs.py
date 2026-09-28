"""Turning an upload into work for the sidecar, and taking it back."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_align.epub import EpubInfo
from cplus_align.protocol import (
    CANCEL_FILE,
    EPUB_FILE,
    JOB_FILE,
    AlignPaths,
    write_json,
)

from ..db.models import AudiobookAlignment, AudiobookJob, AudiobookJobStatus, User
from ..plex.client import PlexAlbum, PlexAudioPart

INTERNAL_URL_ENV = "CPLUS_INTERNAL_URL"

#: How the sidecar reaches this service. Compose puts both on one network under
#: their service names, so the default needs no configuration.
DEFAULT_INTERNAL_URL = "http://cplus-service:8080"

#: Uploads bigger than this are refused before they are read. An epub is text
#: plus images; the largest real ones are a few tens of MB.
MAX_EPUB_BYTES = 100 * 1024 * 1024


def internal_url() -> str:
    return (os.environ.get(INTERNAL_URL_ENV) or DEFAULT_INTERNAL_URL).rstrip("/")


def track_payload(parts: list[PlexAudioPart]) -> list[dict[str, Any]]:
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


async def latest_jobs(
    db: AsyncSession, server_id: str, rating_keys: list[str]
) -> dict[str, AudiobookJob]:
    """The newest job for each of these books."""
    if not rating_keys:
        return {}
    rows = await db.execute(
        select(AudiobookJob)
        .where(AudiobookJob.plex_server_id == server_id)
        .where(AudiobookJob.rating_key.in_(rating_keys))
        .order_by(AudiobookJob.id)
    )
    return {job.rating_key: job for job in rows.scalars()}


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


async def queue_position(db: AsyncSession, job: AudiobookJob) -> int:
    """1 for the next job to run (or the one running), 2 for the one after, ..."""
    rows = await db.execute(
        select(AudiobookJob.id).where(
            AudiobookJob.status.in_([AudiobookJobStatus.QUEUED, AudiobookJobStatus.RUNNING]),
            AudiobookJob.id < job.id,
        )
    )
    return len(rows.all()) + 1


async def start_job(
    db: AsyncSession,
    target: AlignPaths,
    *,
    server_id: str,
    album: PlexAlbum,
    parts: list[PlexAudioPart],
    epub: bytes,
    info: EpubInfo,
    admin: User,
) -> AudiobookJob:
    """Record a job and hand it to the sidecar.

    Commits the row *before* writing the files: the sidecar starts on a job the
    moment ``job.json`` appears, and its first request — for the audio — is
    authorised against this row, so the row must already be visible. ``job.json``
    is written last, since its presence is what marks the directory complete.
    """
    tracks = track_payload(parts)
    job = AudiobookJob(
        plex_server_id=server_id,
        rating_key=album.rating_key,
        library_id=album.library_id,
        title=album.title,
        author=album.author,
        status=AudiobookJobStatus.VERIFYING,
        stage="Waiting for the aligner",
        progress=0.0,
        secret=secrets.token_urlsafe(32),
        tracks=tracks,
        epub_title=info.title,
        epub_author=", ".join(info.authors) or None,
        epub_words=info.words,
        created_by=admin.id,
    )
    db.add(job)
    await db.commit()

    folder = target.job(job.id)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / EPUB_FILE).write_bytes(epub)
    base = internal_url()
    write_json(
        folder / JOB_FILE,
        {
            "version": 1,
            "id": job.id,
            "title": album.title,
            "duration": sum(t["duration"] for t in tracks),
            "headers": {"X-Aligner-Key": job.secret},
            "tracks": [
                {
                    "n": t["n"],
                    "rating_key": t["rating_key"],
                    "url": f"{base}/internal/aligner/jobs/{job.id}/tracks/{t['n']}",
                    "size": t["size"],
                    "duration": t["duration"],
                    "container": t["container"],
                }
                for t in tracks
            ],
        },
    )
    return job


def cancel_job(target: AlignPaths | None, job: AudiobookJob) -> None:
    """Stop a job. The sidecar notices within a second; the monitor tidies up after."""
    if target is not None:
        folder = target.job(job.id)
        if folder.exists():
            (folder / CANCEL_FILE).write_text(str(time.time()))
    job.status = AudiobookJobStatus.CANCELLED
    job.stage = None
    job.finished_at = datetime.now(UTC)
