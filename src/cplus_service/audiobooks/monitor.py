"""Following the sidecar's files and keeping job rows current.

A background task, started in the app's lifespan when an aligner directory is
configured. Every few seconds it reads each active job's files, updates its
row, ingests a finished result, and deletes the working directory of every job
that has ended — the uploaded epub goes with it. Reading the files here rather
than on page load is what lets a result be stored while nobody has the tab
open.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cplus_align.protocol import (
    ERROR_FILE,
    RESULT_FILE,
    STATUS_FILE,
    VERIFY_FILE,
    AlignPaths,
    read_json,
)

from ..db.models import AudiobookJob, AudiobookJobStatus
from ..db.session import session_scope
from ..web.copy_strings import text
from .ingest import InvalidResult, build_alignment, load_result, replace_alignment, summary

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0

ACTIVE = [AudiobookJobStatus.VERIFYING, AudiobookJobStatus.QUEUED, AudiobookJobStatus.RUNNING]


def _utc(stamp: Any) -> datetime | None:
    if not isinstance(stamp, int | float):
        return None
    return datetime.fromtimestamp(stamp, UTC)


def _read_files(paths: AlignPaths, job_id: int) -> dict[str, Any]:
    folder = paths.job(job_id)
    return {
        "exists": folder.exists(),
        "error": read_json(folder / ERROR_FILE),
        "verify": read_json(folder / VERIFY_FILE),
        "status": read_json(folder / STATUS_FILE),
        "result": (folder / RESULT_FILE).exists(),
    }


async def sync_job(db: AsyncSession, paths: AlignPaths, job: AudiobookJob) -> None:
    files = await asyncio.to_thread(_read_files, paths, job.id)
    now = datetime.now(UTC)
    status = files["status"] or {}
    heard = _utc(status.get("t"))
    if heard:
        job.heard_at = heard

    if not files["exists"]:
        job.status = AudiobookJobStatus.FAILED
        job.message = text("py_audiobooks.job_disappeared.text")
        job.finished_at = now
        return

    error = files["error"]
    if error:
        cancelled = error.get("code") == "cancelled"
        job.status = AudiobookJobStatus.CANCELLED if cancelled else AudiobookJobStatus.FAILED
        if not cancelled:
            job.message = error.get("message") or text("py_audiobooks.aligner_stopped.text")
        job.stage = None
        job.finished_at = now
        return

    if files["result"]:
        try:
            result = await asyncio.to_thread(load_result, paths.job(job.id) / RESULT_FILE)
        except InvalidResult as exc:
            job.status = AudiobookJobStatus.FAILED
            job.message = text("py_audiobooks.result_unreadable.text", error=exc)
            job.finished_at = now
            return
        alignment = await asyncio.to_thread(build_alignment, job, result)
        await replace_alignment(db, alignment)
        stats = result.get("stats") or {}
        job.status = AudiobookJobStatus.DONE
        job.progress = 100.0
        job.stage = None
        job.eta_seconds = None
        job.finished_at = now
        job.message = summary(stats) or job.message
        logger.info("audiobook %s (%s) aligned", job.title, job.rating_key)
        return

    verify = files["verify"]
    if verify is None:
        job.status = AudiobookJobStatus.VERIFYING
        if status.get("phase") == "verifying":
            job.progress = float(status.get("pct") or 0.0)
            job.stage = status.get("label") or text("py_audiobooks.stage_verifying.text")
        return

    if not verify.get("ok"):
        job.status = AudiobookJobStatus.FAILED
        job.message = verify.get("message") or text("py_audiobooks.epub_mismatch.text")
        job.finished_at = now
        return

    job.message = verify.get("warning")
    if status.get("phase") == "running":
        if job.status != AudiobookJobStatus.RUNNING:
            job.started_at = job.started_at or now
        job.status = AudiobookJobStatus.RUNNING
        job.progress = float(status.get("pct") or 0.0)
        job.eta_seconds = status.get("eta")
        job.stage = status.get("label") or None
    else:
        job.status = AudiobookJobStatus.QUEUED
        job.progress = 0.0
        job.stage = None


#: A directory is only swept once nothing has touched it for this long. An upload
#: writes its directory before its row commits, and a cancelled job's engine
#: may still be writing for a second or two after the row says it stopped.
SWEEP_QUIET_SECONDS = 60.0


def _sweep(paths: AlignPaths, keep: set[int]) -> None:
    """Delete every job directory that is not an active job's."""
    now = time.time()
    for job_id in paths.job_ids():
        if job_id in keep:
            continue
        folder = paths.job(job_id)
        try:
            if now - folder.stat().st_mtime < SWEEP_QUIET_SECONDS:
                continue
        except FileNotFoundError:
            continue
        shutil.rmtree(folder, ignore_errors=True)


async def sync_once(sessionmaker: async_sessionmaker[AsyncSession], paths: AlignPaths) -> None:
    async with session_scope(sessionmaker) as db:
        rows = await db.execute(select(AudiobookJob).where(AudiobookJob.status.in_(ACTIVE)))
        for job in rows.scalars().all():
            try:
                await sync_job(db, paths, job)
            except Exception:  # noqa: BLE001 - one bad job must not stall the rest
                logger.exception("syncing audiobook job %s failed", job.id)
        await db.flush()
        rows = await db.execute(select(AudiobookJob.id).where(AudiobookJob.status.in_(ACTIVE)))
        keep = {row[0] for row in rows.all()}
    await asyncio.to_thread(_sweep, paths, keep)


async def run(sessionmaker: async_sessionmaker[AsyncSession], paths: AlignPaths) -> None:
    logger.info("following aligner jobs in %s", paths.root)
    while True:
        try:
            await sync_once(sessionmaker, paths)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("aligner monitor pass failed")
        await asyncio.sleep(POLL_SECONDS)
