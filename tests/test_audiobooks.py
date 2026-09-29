"""The Audiobooks tab, the job monitor and the sidecar's audio route.

Real ASGI app and database; Plex is mocked with respx, and the sidecar is played
by writing its files into a temporary aligner directory the way it would.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_align.protocol import (
    ALIGN_DIR_ENV,
    AlignPaths,
    read_json,
    write_json,
)
from cplus_service.audiobooks import monitor
from cplus_service.audiobooks.ingest import build_alignment, chunk_sentences, decode_chunk
from cplus_service.db.models import (
    AudiobookAlignment,
    AudiobookChunk,
    AudiobookJob,
    AudiobookJobStatus,
    Config,
)
from cplus_service.db.session import get_config

from .audiobook_fixtures import (
    ALBUM_KEY,
    CONTENT_ENCRYPTION,
    PLEX_SERVER_URL,
    SERVER_ID,
    album_payload,
    albums_payload,
    children_payload,
    make_epub,
    result_payload,
    sections_payload,
    sidecar_up,
    write_result,
)
from .test_admin_webui import signed_in


@pytest.fixture
async def connected(db: AsyncSession) -> Config:
    config = await get_config(db)
    config.plex_admin_token = "server-token"
    config.plex_server_base_url = PLEX_SERVER_URL
    config.plex_server_client_identifier = SERVER_ID
    config.plex_server_name = "Test Server"
    await db.commit()
    return config


@pytest.fixture
def paths(app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AlignPaths:
    """An aligner directory, set up *after* the app started.

    Depending on ``app`` first keeps the lifespan from starting the background
    monitor, so each test drives :func:`monitor.sync_once` itself.
    """
    root = tmp_path / "align"
    root.mkdir()
    monkeypatch.setenv(ALIGN_DIR_ENV, str(root))
    return AlignPaths(root)


@pytest.fixture
def plex():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(200, json=sections_payload())
        )
        mock.get(f"{PLEX_SERVER_URL}/library/sections/7/all").mock(
            return_value=httpx.Response(200, json=albums_payload(album_payload()))
        )
        mock.get(f"{PLEX_SERVER_URL}/library/metadata/{ALBUM_KEY}").mock(
            return_value=httpx.Response(
                200, json={"MediaContainer": {"Metadata": [album_payload()]}}
            )
        )
        mock.get(f"{PLEX_SERVER_URL}/library/metadata/{ALBUM_KEY}/children").mock(
            return_value=httpx.Response(200, json=children_payload())
        )
        yield mock


async def upload(client: httpx.AsyncClient, data: bytes, key: str = ALBUM_KEY) -> httpx.Response:
    return await client.post(
        f"/admin/audiobooks/books/{key}/align",
        files={"epub": ("book.epub", data, "application/epub+zip")},
    )


async def jobs(db: AsyncSession) -> list[AudiobookJob]:
    db.expire_all()
    return list((await db.execute(select(AudiobookJob).order_by(AudiobookJob.id))).scalars())


# --------------------------------------------------------------------------- #
# The page and the runtime card
# --------------------------------------------------------------------------- #


async def test_the_tab_says_read_along_is_not_set_up_without_an_aligner(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, monkeypatch
) -> None:
    monkeypatch.delenv(ALIGN_DIR_ENV, raising=False)
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert response.status_code == 200
    assert "isn't set up on this deployment" in response.text
    assert "Align…" not in response.text


async def test_the_tab_lists_only_music_libraries_and_their_albums(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths, status="absent")
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert response.status_code == 200
    assert '<option value="7"' in response.text and '<option value="8"' in response.text
    assert '<option value="1"' not in response.text  # the movie library
    assert "The Hobbit" in response.text and "J. R. R. Tolkien" in response.text


async def test_enabling_is_offered_with_its_download_size_and_licence(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths, status="absent")
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert "Turn on Canopy+ Audiobooks" in response.text
    assert "GB</strong> once" in response.text
    assert "CC BY-NC 4.0" in response.text
    # Nothing can be aligned until it's on.
    assert "Align…" not in response.text


def low_disk(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    from collections import namedtuple

    from cplus_service.audiobooks import runtime

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda _path: usage(free, 0, free))


async def test_enabling_is_disabled_without_10_gb_free(
    client: httpx.AsyncClient,
    db: AsyncSession,
    connected: Config,
    plex,
    paths: AlignPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar_up(paths, status="absent")
    low_disk(monkeypatch, 4_200_000_000)
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert "Canopy+ Audiobooks requires 10.0 GB of disk space (4.2 GB available)" in " ".join(
        response.text.split()
    )
    assert "disabled>" in " ".join(response.text.split())

    refused = await client.post("/admin/audiobooks/runtime/enable")
    assert refused.status_code == 409
    assert "4.2 GB available" in refused.text
    assert not paths.request_file.exists()


async def test_enabling_asks_the_sidecar_to_install(
    client: httpx.AsyncClient, db: AsyncSession, paths: AlignPaths
) -> None:
    sidecar_up(paths, status="absent")
    await signed_in(client, db)
    response = await client.post("/admin/audiobooks/runtime/enable")
    assert response.status_code == 200
    assert read_json(paths.request_file)["action"] == "install"
    assert "Starting…" in response.text or "Downloading" in response.text


async def test_a_sidecar_that_stopped_checking_in_reads_as_offline(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    write_json(paths.heartbeat_file, {"t": time.time() - 3600})
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert "isn't running" in response.text
    enable = await client.post("/admin/audiobooks/runtime/enable")
    assert enable.status_code == 409


async def test_a_runtime_from_an_older_image_asks_for_an_update(
    client: httpx.AsyncClient, db: AsyncSession, paths: AlignPaths
) -> None:
    sidecar_up(paths, runtime="something-older")
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks/runtime")
    assert "needs an updated aligner runtime" in response.text


async def test_the_runtime_card_reloads_the_page_when_an_install_finishes(
    client: httpx.AsyncClient, db: AsyncSession, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks/runtime?was=installing")
    assert response.headers.get("HX-Refresh") == "true"
    unchanged = await client.get("/admin/audiobooks/runtime?was=ready")
    assert "HX-Refresh" not in unchanged.headers


async def test_turning_off_cancels_work_and_asks_for_removal(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    await upload(client, make_epub())
    response = await client.post("/admin/audiobooks/runtime/disable")
    assert response.status_code == 200
    assert read_json(paths.request_file)["action"] == "uninstall"
    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.CANCELLED
    assert (paths.job(job.id) / "cancel").exists()


# --------------------------------------------------------------------------- #
# Uploading
# --------------------------------------------------------------------------- #


async def test_an_upload_becomes_a_verifying_job_the_sidecar_can_pick_up(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    response = await upload(client, make_epub())

    assert response.status_code == 200
    assert "Verifying…" in response.text
    assert 'hx-trigger="every 2s"' in response.text

    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.VERIFYING
    assert job.rating_key == ALBUM_KEY and job.plex_server_id == SERVER_ID
    assert job.epub_title == "The Hobbit" and job.epub_words > 1000

    spec = read_json(paths.job(job.id) / "job.json")
    assert spec["headers"] == {"X-Aligner-Key": job.secret}
    assert spec["tracks"][0]["url"].endswith(f"/internal/aligner/jobs/{job.id}/tracks/0")
    assert spec["tracks"][0]["size"] == 600_000_000
    assert spec["duration"] == pytest.approx(37500.0)
    assert (paths.job(job.id) / "book.epub").read_bytes()[:2] == b"PK"
    # The Plex token never reaches the sidecar.
    assert "server-token" not in json.dumps(spec)


@pytest.mark.parametrize(
    ("epub", "complaint"),
    [
        (make_epub(encryption=CONTENT_ENCRYPTION), "DRM-protected"),
        (make_epub(language="fr"), "Only English"),
        (make_epub(paragraphs=2), "almost no readable text"),
        (b"not a zip at all", "not a zip archive"),
    ],
)
async def test_an_unusable_epub_is_refused_before_anything_is_queued(
    client: httpx.AsyncClient,
    db: AsyncSession,
    connected: Config,
    plex,
    paths: AlignPaths,
    epub: bytes,
    complaint: str,
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    response = await upload(client, epub)
    assert response.status_code == 200
    assert complaint in response.text
    assert await jobs(db) == []
    assert paths.job_ids() == []


async def test_uploading_is_refused_while_read_along_is_off(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths, status="absent")
    await signed_in(client, db)
    response = await upload(client, make_epub())
    assert "turned on, so nothing can be aligned" in response.text
    assert await jobs(db) == []


async def test_a_second_upload_waits_for_the_first_to_finish(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    await upload(client, make_epub())
    response = await upload(client, make_epub())
    assert "already being aligned" in response.text
    assert len(await jobs(db)) == 1


async def test_cancelling_marks_the_job_and_tells_the_sidecar(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    await upload(client, make_epub())
    response = await client.post(f"/admin/audiobooks/books/{ALBUM_KEY}/cancel")
    assert response.status_code == 200
    assert "Align…" in response.text
    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.CANCELLED
    assert (paths.job(job.id) / "cancel").exists()


# --------------------------------------------------------------------------- #
# Following the sidecar
# --------------------------------------------------------------------------- #


async def _job(client, db, paths) -> AudiobookJob:
    sidecar_up(paths)
    await signed_in(client, db)
    await upload(client, make_epub())
    [job] = await jobs(db)
    return job


async def _sync(app: FastAPI, paths: AlignPaths) -> None:
    await monitor.sync_once(app.state.cplus.sessionmaker, paths)


async def test_verification_progress_reaches_the_cell(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    write_json(
        paths.job(job.id) / "status.json",
        {"t": time.time(), "phase": "verifying", "label": "Reading audio samples", "pct": 40},
    )
    await _sync(app, paths)
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "Verifying…" in response.text
    assert "Reading audio samples" in response.text
    assert "width: 40.0%" in response.text


async def test_a_failed_verification_says_why_and_offers_another_upload(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    write_json(
        paths.job(job.id) / "verify.json",
        {"ok": False, "message": "This epub doesn't match the audio: 0 of 8 samples."},
    )
    await _sync(app, paths)
    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.FAILED
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "match the audio: 0 of 8" in response.text
    assert "Try another epub…" in response.text
    assert "hx-trigger" not in response.text.split("upload-form")[0]  # stopped polling


async def test_a_passed_verification_queues_then_runs_with_progress_and_eta(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    folder = paths.job(job.id)
    write_json(folder / "verify.json", {"ok": True, "message": "8 of 8 samples matched."})
    await _sync(app, paths)
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "Processing…" in response.text and "starting" in response.text

    write_json(
        folder / "status.json",
        {"t": time.time(), "phase": "running", "label": "Listening to the audio", "pct": 43.2,
         "eta": 3 * 3600 + 1234},
    )  # fmt: skip
    await _sync(app, paths)
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "Processing… 43%" in response.text
    assert "about 3 h 20 min left" in response.text
    assert 'hx-trigger="every 10s"' in response.text


async def test_a_running_job_that_goes_quiet_is_shown_as_stalled(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    folder = paths.job(job.id)
    write_json(folder / "verify.json", {"ok": True})
    write_json(
        folder / "status.json",
        {"t": time.time() - 900, "phase": "running", "label": "x", "pct": 50, "eta": 60},
    )
    await _sync(app, paths)
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "No word from the aligner for 15 minutes" in response.text


async def test_a_result_is_stored_in_chunks_and_the_job_directory_removed(
    app, client, db, connected, plex, paths, monkeypatch
) -> None:
    job = await _job(client, db, paths)
    folder = paths.job(job.id)
    write_json(folder / "verify.json", {"ok": True})
    write_result(folder)
    await _sync(app, paths)

    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.DONE
    assert "5 of 6 sentences aligned" in job.message
    alignment = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert alignment.rating_key == ALBUM_KEY and alignment.plex_server_id == SERVER_ID
    assert alignment.tracks[0]["offset"] == 0.0 and alignment.tracks[0]["rating_key"] == "502"
    assert [s["title"] for s in alignment.sections] == ["One", "Two"]
    chunks = list((await db.execute(select(AudiobookChunk))).scalars())
    sentences = [s for c in chunks for s in decode_chunk(c.data)]
    assert [s["i"] for s in sentences] == list(range(6))
    assert sentences[1]["start"] is None and sentences[1]["flags"] == ["unspoken"]

    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "✓ Ready" in response.text and "5 of 6 sentences" in response.text

    # The epub and everything else in the job's directory go once it is quiet.
    monkeypatch.setattr(monitor, "SWEEP_QUIET_SECONDS", 0)
    await _sync(app, paths)
    assert not folder.exists()


async def test_the_sweep_leaves_a_fresh_directory_alone(app, paths: AlignPaths) -> None:
    orphan = paths.job(999)
    orphan.mkdir(parents=True)
    await _sync(app, paths)
    assert orphan.exists()


async def test_a_replacement_keeps_the_old_alignment_until_it_succeeds(
    app, client, db, connected, plex, paths
) -> None:
    first = await _job(client, db, paths)
    first_id = first.id
    write_json(paths.job(first_id) / "verify.json", {"ok": True})
    write_result(paths.job(first_id))
    await _sync(app, paths)

    await upload(client, make_epub())
    second = (await jobs(db))[-1]
    write_json(paths.job(second.id) / "verify.json", {"ok": False, "message": "Wrong book."})
    await _sync(app, paths)

    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/status")
    assert "✓ Ready" in response.text
    assert "go through: Wrong book." in response.text
    db.expire_all()
    assert (await db.execute(select(AudiobookAlignment))).scalar_one().job_id == first_id


async def test_changed_audio_in_plex_marks_an_alignment_out_of_date(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    write_json(paths.job(job.id) / "verify.json", {"ok": True})
    write_result(paths.job(job.id))
    await _sync(app, paths)

    page = await client.get("/admin/audiobooks")
    assert "has changed since this was aligned" not in page.text

    plex.get(f"{PLEX_SERVER_URL}/library/metadata/{ALBUM_KEY}/children").mock(
        return_value=httpx.Response(200, json=children_payload(part_id=9002))
    )
    page = await client.get("/admin/audiobooks")
    assert "has changed since this was aligned" in page.text


async def test_deleting_an_alignment_removes_its_chunks(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    write_json(paths.job(job.id) / "verify.json", {"ok": True})
    write_result(paths.job(job.id))
    await _sync(app, paths)
    response = await client.post(f"/admin/audiobooks/books/{ALBUM_KEY}/delete")
    assert "Align…" in response.text
    db.expire_all()
    assert (await db.execute(select(AudiobookAlignment))).first() is None
    assert (await db.execute(select(AudiobookChunk))).first() is None


async def test_a_sidecar_error_fails_the_job_with_its_message(
    app, client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    write_json(paths.job(job.id) / "verify.json", {"ok": True})
    write_json(
        paths.job(job.id) / "error.json",
        {"code": "crashed", "message": "The aligner was killed while working on this book."},
    )
    await _sync(app, paths)
    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.FAILED
    assert "killed" in job.message


# --------------------------------------------------------------------------- #
# The sidecar's audio route
# --------------------------------------------------------------------------- #


async def test_the_audio_route_relays_plex_with_range_and_token(
    client, db, connected, plex, paths
) -> None:
    job = await _job(client, db, paths)
    route = plex.get(f"{PLEX_SERVER_URL}/library/parts/9001/1690000000/file.mp3").mock(
        return_value=httpx.Response(
            206,
            content=b"abcd",
            headers={"Content-Range": "bytes 100-103/600000000", "Accept-Ranges": "bytes",
                     "Content-Type": "audio/mpeg", "X-Plex-Protocol": "1.0"},
        )
    )  # fmt: skip
    client.cookies.clear()  # the sidecar has no session
    response = await client.get(
        f"/internal/aligner/jobs/{job.id}/tracks/0",
        headers={"X-Aligner-Key": job.secret, "Range": "bytes=100-103"},
    )
    assert response.status_code == 206
    assert response.content == b"abcd"
    assert response.headers["content-range"] == "bytes 100-103/600000000"
    assert "x-plex-protocol" not in response.headers
    sent = route.calls.last.request
    assert sent.headers["Range"] == "bytes=100-103"
    assert sent.headers["X-Plex-Token"] == "server-token"


@pytest.mark.parametrize("variant", ["wrong key", "no key", "no such track", "finished job"])
async def test_the_audio_route_answers_404_to_anything_but_an_active_jobs_key(
    client, db, connected, plex, paths, variant: str
) -> None:
    job = await _job(client, db, paths)
    headers = {"X-Aligner-Key": job.secret}
    track = 0
    if variant == "wrong key":
        headers = {"X-Aligner-Key": "nope"}
    elif variant == "no key":
        headers = {}
    elif variant == "no such track":
        track = 5
    else:
        await client.post(f"/admin/audiobooks/books/{ALBUM_KEY}/cancel")
    response = await client.get(f"/internal/aligner/jobs/{job.id}/tracks/{track}", headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def _sentence(i: int, para: int, start: float | None) -> dict:
    return {"i": i, "para": para, "sec": 0, "text": "x", "start": start,
            "end": None if start is None else start + 3, "flags": []}  # fmt: skip


def test_chunks_close_at_the_first_paragraph_break_after_ten_minutes() -> None:
    sentences = [_sentence(i, i // 3, i * 30.0) for i in range(60)]  # 30 min of audio
    chunks = chunk_sentences(sentences)
    assert [c["first"] for c in chunks] == [0, 21, 42]
    # Never mid-paragraph: every chunk starts where a paragraph does.
    assert all(c["first"] % 3 == 0 for c in chunks)
    assert chunks[0]["start"] == 0.0 and chunks[0]["end"] == 20 * 30.0 + 3


def test_a_long_unspoken_stretch_still_gets_chunked() -> None:
    sentences = [_sentence(i, i, None) for i in range(2000)]
    chunks = chunk_sentences(sentences)
    assert len(chunks) == 3
    assert chunks[0]["start"] is None and chunks[0]["end"] is None


def test_chunk_bytes_are_reproducible() -> None:
    from cplus_service.audiobooks.ingest import encode_chunk

    payload = [_sentence(0, 0, 1.0)]
    assert encode_chunk(payload) == encode_chunk(payload)
    assert json.loads(gzip.decompress(encode_chunk(payload)))[0]["start"] == 1.0


def test_result_fixture_is_valid() -> None:
    assert result_payload()["version"] == 1


# --------------------------------------------------------------------------- #
# Uploading a finished alignment
# --------------------------------------------------------------------------- #


def bookalign_output(*, duration: float = 37500.0, sentences: int = 6) -> bytes:
    """What ``bookalign.py`` writes: no per-file offsets, no authors, extra fields."""
    payload = result_payload(sentences=sentences)
    payload["book"] = {"title": "The Hobbit", "epub": "The Hobbit - J. R. R. Tolkien.epub"}
    payload["audio"] = {"file": "The Hobbit.mp3", "duration": duration}
    for sentence in payload["sentences"]:
        if sentence["start"] is not None:
            sentence.update(min_score=-2.1, worst="hobbit")
    payload["stats"].update(device="mps", model="MahmoudAshraf/mms-300m-1130-forced-aligner")
    return json.dumps(payload).encode()


async def import_json(
    client: httpx.AsyncClient, data: bytes, name: str = "hobbit.alignment.json"
) -> httpx.Response:
    return await client.post(
        f"/admin/audiobooks/books/{ALBUM_KEY}/import",
        files={"alignment": (name, data, "application/json")},
    )


async def test_a_bookalign_json_is_imported_without_the_aligner_at_all(
    client, db, connected, plex, monkeypatch
) -> None:
    monkeypatch.delenv(ALIGN_DIR_ENV, raising=False)  # read-along not even set up
    await signed_in(client, db)
    response = await import_json(client, bookalign_output())
    assert response.status_code == 200
    assert "✓ Ready" in response.text and "5 of 6 sentences" in response.text

    db.expire_all()
    alignment = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert alignment.tracks == [
        {"n": 0, "rating_key": "502", "part_id": "9001", "offset": 0.0, "duration": 37500.0}
    ]
    assert alignment.epub_title == "The Hobbit"
    [job] = await jobs(db)
    assert job.status == AudiobookJobStatus.DONE
    assert job.message == "Imported from hobbit.alignment.json. 5 of 6 sentences aligned."
    chunk = (await db.execute(select(AudiobookChunk))).scalars().first()
    assert decode_chunk(chunk.data)[0]["text"] == "Sentence 0."


async def test_an_import_replaces_the_existing_alignment_under_a_new_version(
    client, db, connected, plex
) -> None:
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    db.expire_all()
    first = (await db.execute(select(AudiobookAlignment))).scalar_one().id
    await import_json(client, bookalign_output(sentences=8))
    db.expire_all()
    second = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert second.id != first
    assert second.stats["sentences"] == 8


async def test_an_alignment_of_different_audio_is_refused(client, db, connected, plex) -> None:
    await signed_in(client, db)
    response = await import_json(client, bookalign_output(duration=3346.0))
    assert "56 min of audio, but Plex has 10 h 25 min" in response.text
    assert await jobs(db) == []


async def test_a_many_file_album_needs_the_alignments_own_offsets(
    client, db, connected, plex
) -> None:
    two_files = children_payload()
    first = two_files["MediaContainer"]["Metadata"][0]
    second = json.loads(json.dumps(first))
    first["duration"] = first["Media"][0]["Part"][0]["duration"] = 20_000_000
    second.update(ratingKey="503", index=2)
    second["duration"] = second["Media"][0]["Part"][0]["duration"] = 17_500_000
    second["Media"][0]["Part"][0].update(id=9002, key="/library/parts/9002/1/file.mp3")
    two_files["MediaContainer"]["Metadata"].append(second)
    plex.get(f"{PLEX_SERVER_URL}/library/metadata/{ALBUM_KEY}/children").mock(
        return_value=httpx.Response(200, json=two_files)
    )
    await signed_in(client, db)

    refused = await import_json(client, bookalign_output())
    assert "Plex has 2 files for this book" in refused.text

    with_offsets = json.loads(bookalign_output())
    with_offsets["audio"]["tracks"] = [
        {"n": 0, "offset": 0.0, "duration": 19999.95},
        {"n": 1, "offset": 19999.95, "duration": 17500.05},
    ]
    accepted = await import_json(client, json.dumps(with_offsets).encode())
    assert "✓ Ready" in accepted.text
    db.expire_all()
    alignment = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert [t["offset"] for t in alignment.tracks] == [0.0, 19999.95]
    assert [t["rating_key"] for t in alignment.tracks] == ["502", "503"]


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"{not json", "not JSON"),
        (json.dumps({"version": 2, "sentences": []}).encode(), "not a version 1 alignment"),
        (
            json.dumps({"version": 1, "sentences": [{"i": 1, "text": "x"}]}).encode(),
            "sentence 0 is missing or out of order",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "audio": {"duration": 37500.0},
                    "sentences": [
                        {"i": 0, "sec": 0, "para": 0, "text": "x", "start": 9.0, "end": 3.0}
                    ],
                }  # fmt: skip
            ).encode(),
            "impossible times",
        ),
    ],
)
async def test_something_that_is_not_an_alignment_is_refused(
    client, db, connected, plex, data: bytes, complaint: str
) -> None:
    await signed_in(client, db)
    response = await import_json(client, data)
    assert complaint in response.text
    assert await jobs(db) == []


async def test_importing_waits_for_a_job_in_flight(
    client, db, connected, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    await upload(client, make_epub())
    response = await import_json(client, bookalign_output())
    assert "Cancel that first" in response.text
    assert len(await jobs(db)) == 1


async def test_the_menu_is_offered_except_while_a_job_is_in_flight(
    client, db, connected, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    page = await client.get("/admin/audiobooks")
    assert 'class="book-menu"' in page.text and "Upload alignment JSON…" in page.text
    verifying = await upload(client, make_epub())
    assert "book-menu" not in verifying.text


async def test_a_refused_upload_on_a_finished_book_still_shows_it_finished(
    client, db, connected, plex, paths: AlignPaths
) -> None:
    sidecar_up(paths)
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    refused_json = await import_json(client, b"{not json")
    assert "not JSON" in refused_json.text and "✓ Ready" in refused_json.text
    refused_epub = await upload(client, make_epub(language="fr"))
    assert "Only English" in refused_epub.text and "✓ Ready" in refused_epub.text
    assert "Try another epub" not in refused_epub.text


def test_chapter_details_the_aligner_writes_reach_clients() -> None:
    from types import SimpleNamespace

    TITLE = "Book One \u00b7 Chapter 1: Roast Mutton"
    result = result_payload()
    result["sections"] = [
        {
            "index": 0, "title": TITLE, "start": 0.0, "end": 30.0, "sentences": 3,
            "range": [0, 3], "label": "Chapter 1", "number": 1, "part": "Book One",
            "name": "Roast Mutton",
        },
        {"index": 1, "title": "Two", "start": 30.0, "end": 54.0, "sentences": 3},
    ]  # fmt: skip
    track = {"n": 0, "rating_key": "502", "part_id": "9001", "size": 1, "duration": 37499.9}
    job = SimpleNamespace(
        plex_server_id=SERVER_ID, rating_key=ALBUM_KEY, library_id="7", title="T", author="A",
        tracks=[track], id=1, epub_title=None, epub_author=None,
    )  # fmt: skip
    first, second = build_alignment(job, result).sections  # type: ignore[arg-type]
    assert first == {
        "index": 0, "title": TITLE, "start": 0.0, "end": 30.0, "sentences": 3,
        "label": "Chapter 1", "number": 1, "part": "Book One", "name": "Roast Mutton",
    }  # fmt: skip
    # An alignment without the extra fields (an older one) is stored exactly as before.
    assert second == {"index": 1, "title": "Two", "start": 30.0, "end": 54.0, "sentences": 3}
