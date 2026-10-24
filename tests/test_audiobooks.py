"""The Audiobooks tab: listing books, and uploading, downloading and deleting alignments.

Real ASGI app and database; Plex is mocked with respx.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.audiobooks.ingest import build_alignment, chunk_sentences, decode_chunk
from cplus_service.db.models import AudiobookAlignment, AudiobookChunk, Config
from cplus_service.db.session import get_config
from cplus_service.plex.client import PlexAlbum

from .audiobook_fixtures import (
    ALBUM_KEY,
    PLEX_SERVER_URL,
    SERVER_ID,
    album_payload,
    albums_payload,
    children_payload,
    result_payload,
    sections_payload,
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


async def alignment_count(db: AsyncSession) -> int:
    db.expire_all()
    return len((await db.execute(select(AudiobookAlignment))).scalars().all())


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


async def test_the_tab_lists_only_music_libraries_and_their_albums(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/audiobooks")
    assert response.status_code == 200
    assert '<option value="7"' in response.text and '<option value="8"' in response.text
    assert '<option value="1"' not in response.text  # the movie library
    assert "The Hobbit" in response.text and "J. R. R. Tolkien" in response.text


async def test_a_book_without_an_alignment_offers_the_upload(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex
) -> None:
    await signed_in(client, db)
    page = await client.get("/admin/audiobooks")
    assert "Upload alignment" in page.text
    assert 'class="popover-menu"' not in page.text  # nothing to replace, download or delete


async def test_there_is_no_aligning_any_more(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, plex
) -> None:
    await signed_in(client, db)
    for path in (
        f"/admin/audiobooks/books/{ALBUM_KEY}/align",
        f"/admin/audiobooks/books/{ALBUM_KEY}/cancel",
        "/admin/audiobooks/runtime/enable",
    ):
        assert (await client.post(path)).status_code in (404, 405)
    assert (await client.get("/internal/aligner/jobs/1/tracks/0")).status_code == 404


async def test_changed_audio_in_plex_marks_an_alignment_out_of_date(
    client, db, connected, plex
) -> None:
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    page = await client.get("/admin/audiobooks")
    assert "has changed since this was aligned" not in page.text

    plex.get(f"{PLEX_SERVER_URL}/library/metadata/{ALBUM_KEY}/children").mock(
        return_value=httpx.Response(200, json=children_payload(part_id=9002))
    )
    page = await client.get("/admin/audiobooks")
    assert "has changed since this was aligned" in page.text


async def test_deleting_an_alignment_removes_its_chunks(client, db, connected, plex) -> None:
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    response = await client.post(f"/admin/audiobooks/books/{ALBUM_KEY}/delete")
    assert "Upload alignment" in response.text
    db.expire_all()
    assert (await db.execute(select(AudiobookAlignment))).first() is None
    assert (await db.execute(select(AudiobookChunk))).first() is None


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


async def test_a_bookalign_json_is_imported(client, db, connected, plex) -> None:
    await signed_in(client, db)
    response = await import_json(client, bookalign_output())
    assert response.status_code == 200
    assert "✓ Ready" in response.text and "%" not in response.text.split("✓ Ready")[1][:80]

    db.expire_all()
    alignment = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert alignment.tracks == [
        {"n": 0, "rating_key": "502", "part_id": "9001", "offset": 0.0, "duration": 37500.0}
    ]
    assert alignment.epub_title == "The Hobbit"
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


async def test_a_downloaded_alignment_uploads_back_unchanged(client, db, connected, plex) -> None:
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    download = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/alignment.json")
    assert download.status_code == 200
    assert 'filename="The Hobbit.alignment.json"' in download.headers["content-disposition"]
    exported = download.json()
    assert exported["version"] == 1 and len(exported["sentences"]) == 6
    assert exported["audio"]["tracks"] == [{"n": 0, "offset": 0.0, "duration": 37500.0}]

    db.expire_all()

    async def snapshot() -> tuple:
        row = (await db.execute(select(AudiobookAlignment))).scalar_one()
        chunks = [c.data for c in (await db.execute(select(AudiobookChunk))).scalars()]
        return row.id, (row.tracks, row.sections, row.stats, chunks)

    before_id, before = await snapshot()
    await import_json(client, download.content)
    db.expire_all()
    after_id, after = await snapshot()
    assert after_id != before_id  # a new version...
    assert after == before  # ...of the same alignment


async def test_downloading_needs_an_alignment(client, db, connected, plex) -> None:
    await signed_in(client, db)
    response = await client.get(f"/admin/audiobooks/books/{ALBUM_KEY}/alignment.json")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("aligned", "shown"),
    [
        (6, '<span class="ok">✓ Ready</span>'),
        (5, '<span class="ok">✓ Ready</span>'),  # 83%
        (4, '<span class="warn">Partial Match 66%</span>'),  # 66.7%, rounded down
        (3, '<span class="warn">Partial Match 50%</span>'),
        (2, '<span class="err">Incorrect Match 33%</span>'),
    ],
)
async def test_a_finished_book_says_how_well_it_matched(
    client, db, connected, plex, aligned: int, shown: str
) -> None:
    await signed_in(client, db)
    payload = json.loads(bookalign_output())
    for sentence in payload["sentences"][aligned:]:
        sentence["start"] = sentence["end"] = None
    payload["stats"].update(sentences=6, aligned=aligned)
    response = await import_json(client, json.dumps(payload).encode())
    assert shown in response.text


async def test_an_alignment_of_different_audio_is_refused(client, db, connected, plex) -> None:
    await signed_in(client, db)
    response = await import_json(client, bookalign_output(duration=3346.0))
    assert "56 min of audio, but Plex has 10 h 25 min" in response.text
    assert await alignment_count(db) == 0


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
    assert await alignment_count(db) == 0


async def test_a_finished_books_menu_offers_replace_download_and_delete(
    client, db, connected, plex
) -> None:
    await signed_in(client, db)
    ready = await import_json(client, bookalign_output())
    menu = ready.text[ready.text.index('class="popover-menu"') :]
    order = ["Replace alignment", "Download alignment JSON", "Delete"]
    assert [menu.index(item) for item in order] == sorted(menu.index(item) for item in order)


async def test_a_refused_upload_on_a_finished_book_still_shows_it_finished(
    client, db, connected, plex
) -> None:
    await signed_in(client, db)
    await import_json(client, bookalign_output())
    db.expire_all()
    before = (await db.execute(select(AudiobookAlignment))).scalar_one()
    refused_json = await import_json(client, b"{not json")
    assert "not JSON" in refused_json.text and "✓ Ready" in refused_json.text
    wrong_book = await import_json(client, bookalign_output(duration=3346.0))
    assert "Plex has 10 h 25 min" in wrong_book.text and "✓ Ready" in wrong_book.text
    db.expire_all()
    after = (await db.execute(select(AudiobookAlignment))).scalar_one()
    assert (after.id, after.stats) == (before.id, before.stats)


def test_chapter_details_the_aligner_writes_reach_clients() -> None:
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
    album = PlexAlbum(
        rating_key=ALBUM_KEY, title="T", author="A", library_id="7", thumb=None, year=None,
        tracks=1,
    )  # fmt: skip
    stored = build_alignment(server_id=SERVER_ID, album=album, tracks=[track], result=result)
    first, second = stored.sections
    assert first == {
        "index": 0, "title": TITLE, "start": 0.0, "end": 30.0, "sentences": 3,
        "label": "Chapter 1", "number": 1, "part": "Book One", "name": "Roast Mutton",
    }  # fmt: skip
    # An alignment without the extra fields (an older one) is stored exactly as before.
    assert second == {"index": 1, "title": "Two", "start": 30.0, "end": 54.0, "sentences": 3}
