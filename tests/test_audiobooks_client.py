"""Read-along for clients: listing, the index, chunks, progress, and who may see what.

Plex is mocked at both levels access depends on: plex.tv's resource list (which
gives the caller their own token for this server) and the server's library
list as asked with that token.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.audiobooks.ingest import build_alignment, replace_alignment
from cplus_service.auth.plex_cache import remember_token
from cplus_service.db.models import AudiobookJob, AudiobookProgress, Config, User
from cplus_service.db.session import get_config

from .audiobook_fixtures import PLEX_SERVER_URL, SERVER_ID, children_payload, result_payload
from .conftest import PLEX_RESOURCES_URL, PLEX_TOKEN, SEERR_URL, seerr_user_payload

USER_SERVER_TOKEN = "their-server-token"


@pytest.fixture
async def connected(db: AsyncSession) -> Config:
    config = await get_config(db)
    config.plex_admin_token = "server-token"
    config.plex_server_base_url = PLEX_SERVER_URL
    config.plex_server_client_identifier = SERVER_ID
    config.plex_client_identifier = "cplus-install"
    await db.commit()
    return config


@pytest.fixture
async def listener(db: AsyncSession) -> User:
    user = User(seerr_user_id=77, plex_username="listener")
    db.add(user)
    await db.flush()
    await remember_token(db, PLEX_TOKEN, user)
    await db.commit()
    return user


def sections(*ids: str) -> dict:
    return {
        "MediaContainer": {
            "Directory": [{"key": i, "title": f"L{i}", "type": "artist"} for i in ids]
        }
    }


@pytest.fixture
def plex():
    """The caller shares library 7 (and only 7) on this server."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(PLEX_RESOURCES_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "name": "Their Friend's Server",
                        "clientIdentifier": SERVER_ID,
                        "provides": "server",
                        "owned": False,
                        "accessToken": USER_SERVER_TOKEN,
                        "connections": [],
                    }
                ],
            )
        )
        mock.get(
            f"{PLEX_SERVER_URL}/library/sections", headers={"X-Plex-Token": USER_SERVER_TOKEN}
        ).mock(return_value=httpx.Response(200, json=sections("7")))
        yield mock


async def aligned(
    db: AsyncSession, *, rating_key: str = "501", library_id: str = "7", title: str = "The Hobbit"
) -> int:
    job = AudiobookJob(
        plex_server_id=SERVER_ID,
        rating_key=rating_key,
        library_id=library_id,
        title=title,
        author="J. R. R. Tolkien",
        status="done",
        secret="s",
        tracks=[{"n": 0, "rating_key": "502", "part_id": "9001", "size": 1, "duration": 37500.0}],
    )
    db.add(job)
    await db.flush()
    alignment = build_alignment(job, result_payload(sentences=6))
    await replace_alignment(db, alignment)
    await db.commit()
    return alignment.id


# --------------------------------------------------------------------------- #
# Listing and /register
# --------------------------------------------------------------------------- #


async def test_the_list_holds_only_books_in_libraries_the_caller_can_see(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db, rating_key="501", library_id="7", title="The Hobbit")
    await aligned(db, rating_key="601", library_id="9", title="Somebody Else's Book")
    response = await client.get("/audiobooks", headers=plex_headers)
    assert response.status_code == 200
    books = response.json()["books"]
    assert [b["rating_key"] for b in books] == ["501"]
    assert books[0]["title"] == "The Hobbit" and books[0]["duration"] == 37500.0
    assert books[0]["aligned_sentences"] == 5 and books[0]["progress"] is None


async def test_a_book_in_a_hidden_library_is_a_404_everywhere(
    client, db, connected, listener, plex, plex_headers
) -> None:
    version = await aligned(db, rating_key="601", library_id="9")
    for path in ("/audiobooks/601", f"/audiobooks/601/chunks/0?v={version}"):
        response = await client.get(path, headers=plex_headers)
        assert response.status_code == 404, path


async def test_someone_the_server_is_not_shared_with_sees_nothing(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    plex.get(PLEX_RESOURCES_URL).mock(return_value=httpx.Response(200, json=[]))
    response = await client.get("/audiobooks", headers=plex_headers)
    assert response.json() == {"books": []}


async def test_plex_being_unreachable_is_a_502_not_an_empty_list(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    plex.get(PLEX_RESOURCES_URL).mock(side_effect=httpx.ConnectError("down"))
    response = await client.get("/audiobooks", headers=plex_headers)
    assert response.status_code == 502


async def test_access_answers_are_cached_between_requests(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    for _ in range(3):
        assert (await client.get("/audiobooks", headers=plex_headers)).status_code == 200
    assert plex.routes[0].call_count == 1  # plex.tv asked once


async def test_an_unregistered_token_is_refused(client, db, connected, plex) -> None:
    response = await client.get("/audiobooks", headers={"X-Plex-Token": "never-registered"})
    assert response.status_code == 401


@respx.mock
async def test_register_says_whether_there_is_anything_to_read(
    client, db, connected, plex_headers
) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload())
    )
    resources = respx.get(PLEX_RESOURCES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "clientIdentifier": SERVER_ID,
                    "provides": "server",
                    "accessToken": USER_SERVER_TOKEN,
                }
            ],
        )
    )
    respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=sections("7"))
    )

    # Nothing aligned: no Plex call at all, and no tab.
    response = await client.get("/register", headers=plex_headers)
    assert response.json()["audiobooks"] is False
    assert resources.call_count == 0

    await aligned(db, library_id="9")
    response = await client.get("/register", headers=plex_headers)
    assert response.json()["audiobooks"] is False  # a library they can't see

    await aligned(db, rating_key="777", library_id="7")
    response = await client.get("/register", headers=plex_headers)
    # Cached "can see 7" from the last call, so this is immediate.
    assert response.json()["audiobooks"] is True


# --------------------------------------------------------------------------- #
# The index and chunks
# --------------------------------------------------------------------------- #


async def test_the_index_has_tracks_chapters_and_chunk_ranges(
    client, db, connected, listener, plex, plex_headers
) -> None:
    version = await aligned(db)
    body = (await client.get("/audiobooks/501", headers=plex_headers)).json()
    assert body["version"] == version
    assert body["tracks"] == [
        {"n": 0, "rating_key": "502", "part_id": "9001", "offset": 0.0, "duration": 37499.9}
    ]
    assert [c["title"] for c in body["chapters"]] == ["One", "Two"]
    assert body["chunks"] == [{"n": 0, "start": 0.0, "end": 54.0, "first": 0, "last": 5}]


async def test_a_chunk_is_passed_through_gzipped_and_cacheable_forever(
    client, db, connected, listener, plex, plex_headers
) -> None:
    version = await aligned(db)
    response = await client.get(
        f"/audiobooks/501/chunks/0?v={version}",
        headers={**plex_headers, "Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "immutable" in response.headers["cache-control"]
    sentences = response.json()  # httpx un-gzips
    assert [s["i"] for s in sentences] == list(range(6))
    assert sentences[1]["start"] is None and sentences[0]["text"] == "Sentence 0."


async def test_a_client_that_does_not_take_gzip_gets_plain_json(
    client, db, connected, listener, plex, plex_headers
) -> None:
    version = await aligned(db)
    response = await client.get(
        f"/audiobooks/501/chunks/0?v={version}",
        headers={**plex_headers, "Accept-Encoding": "identity"},
    )
    assert "content-encoding" not in response.headers
    assert len(response.json()["sentences"]) == 6


async def test_a_stale_version_asks_for_the_index_again(
    client, db, connected, listener, plex, plex_headers
) -> None:
    old = await aligned(db)
    new = await aligned(db)  # re-aligned: same book, new version
    assert new != old
    stale = await client.get(f"/audiobooks/501/chunks/0?v={old}", headers=plex_headers)
    assert stale.status_code == 409
    fresh = await client.get(f"/audiobooks/501/chunks/0?v={new}", headers=plex_headers)
    assert fresh.status_code == 200


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def _at(minutes: float) -> str:
    return (datetime(2026, 9, 1, 20, 0, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


async def put(client, headers, *, position: float, minutes: float, key: str = "501", **extra):
    return await client.put(
        f"/audiobooks/{key}/progress",
        headers=headers,
        json={"position": position, "listened_at": _at(minutes), **extra},
    )


async def test_progress_is_stored_and_listed(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    response = await put(
        client, plex_headers, position=1234.5, minutes=0, track_rating_key="502",
        track_offset=1234.5, device="Living room",
    )  # fmt: skip
    assert response.json()["applied"] is True
    got = (await client.get("/audiobooks/501/progress", headers=plex_headers)).json()
    assert got["progress"]["position"] == 1234.5
    assert got["progress"]["track_rating_key"] == "502"
    listed = (await client.get("/audiobooks", headers=plex_headers)).json()["books"][0]
    assert listed["progress"]["device"] == "Living room"


async def test_the_newest_listen_wins_not_the_last_write(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    await put(client, plex_headers, position=5000, minutes=30)
    # A device that last played an hour ago reports on launch.
    stale = await put(client, plex_headers, position=100, minutes=-30)
    assert stale.json()["applied"] is False
    assert stale.json()["progress"]["position"] == 5000
    newer = await put(client, plex_headers, position=5300, minutes=40)
    assert newer.json()["applied"] is True


async def test_a_clock_far_in_the_future_cannot_freeze_progress(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    await client.put(
        "/audiobooks/501/progress",
        headers=plex_headers,
        json={"position": 10, "listened_at": future},
    )
    now = datetime.now(UTC) + timedelta(seconds=5)
    later = await client.put(
        "/audiobooks/501/progress",
        headers=plex_headers,
        json={"position": 20, "listened_at": now.isoformat()},
    )
    assert later.json()["applied"] is True


async def test_progress_works_for_a_book_that_was_never_aligned(
    client, db, connected, listener, plex, plex_headers
) -> None:
    plex.get(
        f"{PLEX_SERVER_URL}/library/metadata/888", headers={"X-Plex-Token": USER_SERVER_TOKEN}
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [{"ratingKey": "888", "type": "album", "title": "B"}]
                }
            },
        )
    )
    response = await put(client, plex_headers, position=60, minutes=0, key="888")
    assert response.status_code == 200 and response.json()["applied"] is True


async def test_progress_for_an_album_the_caller_cannot_see_is_refused(
    client, db, connected, listener, plex, plex_headers
) -> None:
    plex.get(
        f"{PLEX_SERVER_URL}/library/metadata/999", headers={"X-Plex-Token": USER_SERVER_TOKEN}
    ).mock(return_value=httpx.Response(404))
    response = await put(client, plex_headers, position=60, minutes=0, key="999")
    assert response.status_code == 404


async def test_progress_is_per_user_and_goes_with_them(
    client, db, connected, listener, plex, plex_headers
) -> None:
    await aligned(db)
    await put(client, plex_headers, position=60, minutes=0)
    other = User(seerr_user_id=78, plex_username="other")
    db.add(other)
    await db.flush()
    await remember_token(db, "other-token", other)
    await db.commit()
    theirs = await client.get("/audiobooks/501/progress", headers={"X-Plex-Token": "other-token"})
    assert theirs.json()["progress"] is None

    await db.delete(await db.get(User, listener.id))
    await db.commit()
    assert (await db.execute(select(AudiobookProgress))).first() is None


def test_fixtures_are_consistent() -> None:
    assert json.loads(json.dumps(children_payload()))["MediaContainer"]["Metadata"]
    assert gzip.decompress(gzip.compress(b"x")) == b"x"
