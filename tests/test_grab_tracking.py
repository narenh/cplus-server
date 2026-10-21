"""Grab tracking end to end: ``POST /grab`` → qBittorrent → Plex → ``GET /grabs/{id}``.

The real app, database and background task, with qBittorrent, Plex and
Prowlarr mocked by respx. Timings are shrunk so a whole run takes a fraction of
a second; the phases and their order are the real ones.

The first block is the promise to existing deployments: with tracking off or
unconfigured, and with qBittorrent unreachable or refusing the password, a grab
succeeds or fails exactly as it always has. Tracking is a side job that starts
only after Prowlarr has accepted the grab, and its own failure is reported as
that grab's ``failed`` phase and nowhere else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.api.routes import grabs as grabs_route
from cplus_service.auth.plex_cache import remember_token
from cplus_service.db.models import Config, Grab, User
from cplus_service.tracking.runner import TrackingSettings, TrackingTimings
from cplus_service.tracking.state import GrabPhase, GrabProgress

from .conftest import PLEX_TOKEN, PROWLARR_URL, SEERR_URL, grant, make_action, seerr_user_payload
from .test_admin_webui import signed_in

QBT = "http://qbt.test:8080"
QBT_PASSWORD = "qbt-password-do-not-log"
QBT_SID = "qbt-session-cookie-do-not-log"
PLEX = "http://plex.test:32400"
PLEX_ADMIN_TOKEN = "plex-admin-token-do-not-log"

MIB = 1024 * 1024
TITLE = "Some.Release.2024.1080p.WEB-DL-GRP"
HASH = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
MAIN_SIZE = 50 * MIB
NFO_SIZE = 1000
MAIN_LAST_PIECE = (NFO_SIZE + MAIN_SIZE - 1) // MIB
FOLDER = f"E:\\Movies\\{TITLE}"
MAIN_PATH = f"{FOLDER}\\{TITLE}.mkv"
# How Plex spells the same file: case and slashes differ, and must not matter.
PLEX_PATH = f"e:/movies/{TITLE.lower()}/{TITLE}.MKV"

FAST = TrackingTimings(
    find_timeout=0.3,
    find_interval=0.01,
    header_interval=0.01,
    header_timeout=2.0,
    match_interval=0.01,
    match_timeout=0.5,
    rescan_after=0.15,
    max_consecutive_errors=3,
    priority_grace=0.0,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeQbt:
    """qBittorrent's Web API, enough of it for one torrent."""

    def __init__(
        self,
        *,
        name: str = TITLE,
        torrent_hash: str = HASH,
        appear_after: int = 1,
        header_after: int = 2,
        save_path: str = "E:\\Movies\\",
        added_on: int | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.hash = torrent_hash
        self.appear_after = appear_after
        self.header_after = header_after
        self.save_path = save_path
        self.added_on = int(time.time()) if added_on is None else added_on
        self.seq_dl = False
        self.f_l_piece_prio = False
        self.info_calls = 0
        self.piece_calls = 0
        self.files = files if files is not None else [
            {"index": 0, "name": f"{TITLE}/{TITLE}.nfo", "size": NFO_SIZE, "progress": 0,
             "piece_range": [0, 0]},
            {"index": 1, "name": f"{TITLE}/{TITLE}.mkv", "size": MAIN_SIZE, "progress": 0,
             "piece_range": [0, MAIN_LAST_PIECE]},
        ]

    def torrent(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "name": self.name,
            "added_on": self.added_on,
            "progress": 0.1,
            "dlspeed": 2_500_000,
            "eta": 120,
            "state": "downloading",
            "seq_dl": self.seq_dl,
            "f_l_piece_prio": self.f_l_piece_prio,
            "save_path": self.save_path,
        }

    def info(self, request: httpx.Request) -> httpx.Response:
        self.info_calls += 1
        if self.info_calls <= self.appear_after:
            return httpx.Response(200, json=[])
        hashes = request.url.params.get("hashes")
        if hashes and self.hash not in hashes.split("|"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[self.torrent()])

    def pieces(self, request: httpx.Request) -> httpx.Response:
        self.piece_calls += 1
        done = 2 if self.piece_calls > self.header_after else 0
        return httpx.Response(200, json=[done] * (MAIN_LAST_PIECE + 1))

    def toggle(self, attribute: str) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            setattr(self, attribute, not getattr(self, attribute))
            return httpx.Response(200)

        return handler

    def install(self) -> FakeQbt:
        self.login = respx.post(f"{QBT}/api/v2/auth/login").mock(
            return_value=httpx.Response(
                200, text="Ok.", headers={"set-cookie": f"SID={QBT_SID}; HttpOnly; path=/"}
            )
        )
        respx.get(f"{QBT}/api/v2/torrents/info").mock(side_effect=self.info)
        self.sequential = respx.post(f"{QBT}/api/v2/torrents/toggleSequentialDownload").mock(
            side_effect=self.toggle("seq_dl")
        )
        self.first_last = respx.post(f"{QBT}/api/v2/torrents/toggleFirstLastPiecePrio").mock(
            side_effect=self.toggle("f_l_piece_prio")
        )
        respx.get(f"{QBT}/api/v2/torrents/properties").mock(
            return_value=httpx.Response(
                200,
                json={"save_path": self.save_path, "piece_size": MIB,
                      "pieces_num": MAIN_LAST_PIECE + 1},
            )
        )
        respx.get(f"{QBT}/api/v2/torrents/files").mock(
            return_value=httpx.Response(200, json=self.files)
        )
        respx.get(f"{QBT}/api/v2/torrents/pieceStates").mock(side_effect=self.pieces)
        return self


class FakePlex:
    """The admin's Plex server: two libraries, a refresh, and a growing listing."""

    def __init__(self, *, appear_after: int | None = 2, file: str = PLEX_PATH) -> None:
        self.appear_after = appear_after
        self.file = file
        self.listings = 0

    def all(self, request: httpx.Request) -> httpx.Response:
        self.listings += 1
        items: list[dict[str, Any]] = [
            {"ratingKey": "999", "title": "Older Film",
             "Media": [{"Part": [{"file": "E:\\Movies\\Older\\Older.mkv"}]}]}
        ]
        if self.appear_after is not None and self.listings > self.appear_after:
            items.insert(0, {"ratingKey": "12345", "title": "Some Release",
                             "Media": [{"Part": [{"file": self.file}]}]})
        return httpx.Response(200, json={"MediaContainer": {"Metadata": items}})

    def install(self) -> FakePlex:
        respx.get(f"{PLEX}/library/sections").mock(
            return_value=httpx.Response(
                200,
                json={"MediaContainer": {"Directory": [
                    {"key": "1", "title": "Movies", "type": "movie",
                     "Location": [{"id": 1, "path": "E:\\Movies"}]},
                    {"key": "2", "title": "TV", "type": "show",
                     "Location": [{"id": 2, "path": "F:\\TV"}]},
                ]}},
            )
        )
        self.refresh = respx.get(f"{PLEX}/library/sections/1/refresh").mock(
            return_value=httpx.Response(200)
        )
        self.listing = respx.get(f"{PLEX}/library/sections/1/all").mock(side_effect=self.all)
        respx.get(f"{PLEX}/library/sections/2/refresh").mock(return_value=httpx.Response(200))
        respx.get(f"{PLEX}/library/sections/2/all").mock(
            return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": []}})
        )
        return self


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def tracking(app: FastAPI, db: AsyncSession, configured: Config) -> Config:
    """Tracking switched on, qBittorrent set, and a Plex server known."""
    configured.qbittorrent_url = QBT
    configured.qbittorrent_username = "admin"
    configured.qbittorrent_password = QBT_PASSWORD
    configured.grab_tracking_enabled = True
    configured.plex_admin_token = PLEX_ADMIN_TOKEN
    configured.plex_server_base_url = PLEX
    await db.commit()
    app.state.cplus.grab_tracker.timings = FAST
    return configured


async def grab(
    client: httpx.AsyncClient,
    db: AsyncSession,
    headers: dict[str, str],
    *,
    guid: str = "guid-1",
    search_first: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    """Register, hold an action, optionally search, and grab ``TITLE``."""
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload())
    )
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(return_value=httpx.Response(201, json={}))
    assert (await client.get("/register", headers=headers)).status_code == 200
    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)
    if search_first is not None:
        respx.get(f"{PROWLARR_URL}/api/v1/search").mock(
            return_value=httpx.Response(200, json=search_first)
        )
        await client.get("/titles/tt0111161/actions", headers=headers)
    return await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": guid, "indexer_id": 1,
              "release_title": TITLE, "size_bytes": MAIN_SIZE},
        headers=headers,
    )


async def follow(
    client: httpx.AsyncClient, grab_id: int, headers: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Long-poll a grab to its end, the way the app will; returns the last body and phases."""
    since, phases = 0, []
    async with asyncio.timeout(10):
        while True:
            response = await client.get(
                f"/grabs/{grab_id}", params={"since": since, "wait": 5}, headers=headers
            )
            assert response.status_code == 200, response.text
            body = response.json()
            if not phases or phases[-1] != body["phase"]:
                phases.append(body["phase"])
            since = body["version"]
            if body["phase"] in ("ready", "failed"):
                return body, phases


def qbt_untouched() -> respx.Route:
    return respx.route(host="qbt.test")


# --------------------------------------------------------------------------- #
# Existing deployments: a grab is a grab
# --------------------------------------------------------------------------- #


@respx.mock
async def test_with_tracking_off_a_grab_is_exactly_as_before(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config, plex_headers: dict
) -> None:
    anything_qbt = qbt_untouched()

    response = await grab(client, db, plex_headers)

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": None, "grab_id": None}
    assert not anything_qbt.called
    record = (await db.execute(select(Grab))).scalar_one()
    assert record.tracking_phase is None and record.torrent_hash is None
    assert (await client.get(f"/grabs/{record.id}", headers=plex_headers)).status_code == 404


@respx.mock
async def test_switched_on_without_qbittorrent_configured_tracks_nothing(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    tracking.qbittorrent_url = None
    await db.commit()
    anything_qbt = qbt_untouched()

    response = await grab(client, db, plex_headers)

    assert response.json()["success"] is True
    assert response.json()["grab_id"] is None
    assert not anything_qbt.called
    assert (await client.get("/capabilities")).json()["grab_tracking"] is False


@respx.mock
async def test_a_prowlarr_failure_is_reported_as_before_and_nothing_is_tracked(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    anything_qbt = qbt_untouched()
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload())
    )
    await client.get("/register", headers=plex_headers)
    user = (await db.execute(select(User))).scalar_one()
    action = await make_action(db, "Stream Now")
    await grant(db, user, action)
    respx.post(f"{PROWLARR_URL}/api/v1/search").mock(return_value=httpx.Response(500))

    response = await client.post(
        "/grab",
        json={"action_id": action.id, "release_guid": "g", "indexer_id": 1,
              "release_title": TITLE},
        headers=plex_headers,
    )

    assert response.status_code == 502
    assert response.json()["success"] is False
    assert not anything_qbt.called
    assert app_tracker_is_idle(client)


def app_tracker_is_idle(client: httpx.AsyncClient) -> bool:
    transport = client._transport
    assert isinstance(transport, httpx.ASGITransport)
    return transport.app.state.cplus.grab_tracker.running == 0  # type: ignore[attr-defined]


@respx.mock
async def test_an_unreachable_qbittorrent_fails_tracking_not_the_grab(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    respx.route(host="qbt.test").mock(side_effect=httpx.ConnectError("refused"))

    response = await grab(client, db, plex_headers)

    assert response.status_code == 200
    assert response.json()["success"] is True
    grab_id = response.json()["grab_id"]
    body, _ = await follow(client, grab_id, plex_headers)
    assert body["phase"] == "failed"
    assert body["error"] == "Could not reach qBittorrent."
    record = await db.get(Grab, grab_id)
    await db.refresh(record)
    assert record.tracking_phase == "failed"
    assert record.tracking_error == "Could not reach qBittorrent."


@respx.mock
async def test_a_refused_qbittorrent_password_fails_tracking_not_the_grab(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

    response = await grab(client, db, plex_headers)

    assert response.status_code == 200
    assert response.json()["success"] is True
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)
    assert body["phase"] == "failed"
    assert "username or password" in body["error"]
    assert QBT_PASSWORD not in body["error"]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_grab_is_followed_into_plex(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    qbt = FakeQbt().install()
    plex = FakePlex().install()

    response = await grab(client, db, plex_headers)
    grab_id = response.json()["grab_id"]
    assert response.json()["success"] is True and isinstance(grab_id, int)

    body, phases = await follow(client, grab_id, plex_headers)

    assert body["phase"] == "ready", body
    assert body["rating_key"] == "12345"
    assert body["error"] is None
    assert body["title"] == TITLE
    assert body["grab_id"] == grab_id
    # The phases in order (fast ones may be skipped between two polls).
    order = [p.value for p in GrabPhase if p is not GrabPhase.FAILED]
    assert [order.index(p) for p in phases] == sorted(order.index(p) for p in phases)
    # Header: pieces 0–3 (the file starts 1000 bytes into piece 0) plus the last.
    assert body["header"] == {"have_mb": 4.0, "need_mb": 4.0}  # an MKV: its start, not its end
    assert body["download"] == {"progress": 0.1, "speed_mbps": 20.0, "eta_seconds": 120}

    # Each toggle flipped exactly once, from off to on.
    assert qbt.sequential.call_count == 1 and qbt.first_last.call_count == 1
    assert qbt.seq_dl and qbt.f_l_piece_prio
    assert qbt.login.call_count == 1

    # Plex scanned just the release folder, spelled the way qBittorrent spells it.
    refresh = plex.refresh.calls[0].request
    assert parse_qs(urlsplit(str(refresh.url)).query)["path"] == [FOLDER]
    assert "E%3A%5CMovies%5C" in str(refresh.url)
    assert refresh.headers["x-plex-token"] == PLEX_ADMIN_TOKEN
    assert plex.refresh.call_count == 1  # found before the rescan was due

    record = await db.get(Grab, grab_id)
    await db.refresh(record)
    assert (record.torrent_hash, record.tracking_phase, record.rating_key) == (
        HASH, "ready", "12345"
    )


@respx.mock
async def test_the_torrent_is_found_by_the_hash_its_search_result_carried(
    client: httpx.AsyncClient,
    db: AsyncSession,
    tracking: Config,
    plex_headers: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The torrent's name shares nothing with the release title: only the hash
    the search saw can find it."""
    caplog.set_level(logging.INFO, logger="cplus_service.tracking")
    FakeQbt(name="totally unrelated name").install()
    FakePlex().install()
    result = {"title": TITLE, "guid": "guid-hashy", "indexerId": 1, "size": MAIN_SIZE,
              "infoHash": HASH.upper()}

    response = await grab(client, db, plex_headers, guid="guid-hashy", search_first=[result])
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready"
    assert f"torrent found by hash: {HASH[:8]}" in caplog.text
    assert HASH not in caplog.text  # hashes are cut to 8 characters


@respx.mock
async def test_a_grab_already_in_qbittorrent_is_still_followed(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    """qBittorrent ignores a torrent it already has, so nothing new appears; the
    existing one, matched by name, is the right one."""
    FakeQbt(added_on=1_000_000, appear_after=0).install()
    FakePlex(appear_after=0).install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready"


@respx.mock
async def test_with_no_library_holding_the_folder_every_library_of_the_kind_is_scanned(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    FakeQbt(save_path="Z:\\Downloads\\").install()
    plex = FakePlex(file=f"Z:\\Downloads\\{TITLE}\\{TITLE}.mkv").install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready"
    # The movie library, as a whole: no path, since no library's folder holds it.
    assert "path" not in plex.refresh.calls[0].request.url.params


@respx.mock
async def test_the_timing_log_names_every_step_and_nothing_secret(
    client: httpx.AsyncClient,
    db: AsyncSession,
    tracking: Config,
    plex_headers: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    FakeQbt().install()
    FakePlex().install()

    response = await grab(client, db, plex_headers)
    grab_id = response.json()["grab_id"]
    await follow(client, grab_id, plex_headers)

    text = caplog.text
    for expected in (
        f'grab {grab_id} +',
        f'"{TITLE}": finding_torrent',
        f"torrent found by name: {HASH[:8]}",
        f"main file {TITLE}/{TITLE}.mkv (50.0 MB)",
        "header needs 4 piece(s) of 1.00 MB",
        "start of the file arriving: 4 of 4 piece(s) done",
        f'scan 1: library "Movies" (section 1), folder {FOLDER}',
        "item found: rating_key 12345",
        f"grab {grab_id} tracking done in ",
        "rating_key 12345",
    ):
        assert expected in text, expected
    for secret in (QBT_PASSWORD, QBT_SID, PLEX_ADMIN_TOKEN, PLEX_TOKEN):
        assert secret not in text


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_torrent_that_never_appears_fails(
    client: httpx.AsyncClient,
    db: AsyncSession,
    tracking: Config,
    plex_headers: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="cplus_service.tracking")
    FakeQbt(appear_after=10_000).install()

    response = await grab(client, db, plex_headers)
    grab_id = response.json()["grab_id"]
    body, phases = await follow(client, grab_id, plex_headers)

    assert phases[-1] == "failed"
    assert "never showed this download" in body["error"]
    assert body["rating_key"] is None
    assert f"grab {grab_id} tracking failed after " in caplog.text
    assert "during finding_torrent" in caplog.text


class LateOrderQbt(FakeQbt):
    """A torrent whose adder (Prowlarr) switches the download order on a moment
    after the torrent appears, as it does in practice."""

    def info(self, request: httpx.Request) -> httpx.Response:
        if self.info_calls >= self.appear_after + 3:
            self.seq_dl = self.f_l_piece_prio = True
        return super().info(request)


@respx.mock
async def test_the_download_order_is_left_alone_while_the_adder_sets_it(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tracking: Config,
    plex_headers: dict
) -> None:
    """No toggle in the grace period: a toggle racing Prowlarr's could switch it off."""
    app.state.cplus.grab_tracker.timings = replace(FAST, priority_grace=0.15)
    qbt = LateOrderQbt(header_after=40).install()
    FakePlex().install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready", body
    assert qbt.sequential.call_count == 0 and qbt.first_last.call_count == 0
    assert qbt.seq_dl and qbt.f_l_piece_prio


@respx.mock
async def test_a_first_piece_still_downloading_is_enough_to_scan(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    """Blocks reach the disk as they arrive, so a piece in progress already holds
    the file's first MB: the scan doesn't wait for the piece to finish."""
    qbt = FakeQbt(header_after=10_000).install()
    respx.get(f"{QBT}/api/v2/torrents/pieceStates").mock(
        return_value=httpx.Response(200, json=[1] * (MAIN_LAST_PIECE + 1))
    )
    plex = FakePlex().install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready", body
    assert plex.refresh.call_count >= 1
    assert qbt.piece_calls == 0  # the downloading states came from the override


@respx.mock
async def test_an_item_plex_found_by_itself_is_ready_before_the_header(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tracking: Config,
    plex_headers: dict
) -> None:
    """The start of the file is stuck, but Plex has the file already: ready at once."""
    app.state.cplus.grab_tracker.timings = replace(FAST, early_check_interval=0.02)
    FakeQbt(header_after=10_000).install()
    plex = FakePlex(appear_after=0).install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready", body
    assert body["rating_key"] == "12345"
    assert plex.refresh.call_count == 0  # no scan needed


@respx.mock
async def test_a_stuck_header_is_scanned_anyway_after_the_timeout(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tracking: Config,
    plex_headers: dict
) -> None:
    """A piece that never completes holds nobody up: the scan goes ahead."""
    app.state.cplus.grab_tracker.timings = replace(
        FAST, header_timeout=0.2, early_check_interval=60.0
    )
    FakeQbt(header_after=10_000).install()
    plex = FakePlex(appear_after=0).install()

    response = await grab(client, db, plex_headers)
    body, phases = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "ready", body
    assert "scanning" in phases or "matching" in phases
    assert plex.refresh.call_count == 1


@respx.mock
async def test_plex_never_showing_the_item_fails_after_rescanning(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    FakeQbt().install()
    plex = FakePlex(appear_after=None).install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "failed"
    assert "did not add the file" in body["error"]
    assert plex.refresh.call_count >= 3  # the scan, then a rescan every rescan_after


@respx.mock
async def test_a_torrent_with_no_video_fails(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    FakeQbt(files=[{"index": 0, "name": "R/R.rar", "size": 10, "piece_range": [0, 0]}]).install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["error"] == "This download has no video file for Plex to play."


@respx.mock
async def test_a_torrent_removed_mid_download_fails(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    qbt = FakeQbt(header_after=10_000).install()
    original = qbt.info

    def vanish_once_seen(request: httpx.Request) -> httpx.Response:
        if qbt.piece_calls >= 2:
            return httpx.Response(200, json=[])
        return original(request)

    respx.get(f"{QBT}/api/v2/torrents/info").mock(side_effect=vanish_once_seen)

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["error"] == "The download was removed from qBittorrent."


@respx.mock
async def test_an_unexpected_error_still_ends_in_failed(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config, plex_headers: dict
) -> None:
    FakeQbt(files=[{"index": "not-a-number", "name": "x.mkv"}]).install()

    response = await grab(client, db, plex_headers)
    body, _ = await follow(client, response.json()["grab_id"], plex_headers)

    assert body["phase"] == "failed"
    assert body["error"] == "Tracking stopped unexpectedly (ValueError)."


@respx.mock
async def test_shutdown_cancels_running_tracking(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, tracking: Config,
    plex_headers: dict,
) -> None:
    tracker = app.state.cplus.grab_tracker
    tracker.timings = TrackingTimings(find_timeout=60, find_interval=0.01)
    FakeQbt(appear_after=10_000).install()

    await grab(client, db, plex_headers)
    assert tracker.running == 1

    await tracker.aclose()

    assert tracker.running == 0


# --------------------------------------------------------------------------- #
# The long poll
# --------------------------------------------------------------------------- #


async def test_waiting_returns_at_once_when_the_state_is_already_newer() -> None:
    progress = GrabProgress(grab_id=1, user_id=1, title="t")
    started = time.monotonic()
    await progress.wait_newer(0, 5)
    assert time.monotonic() - started < 0.5


async def test_waiting_wakes_on_the_next_change() -> None:
    progress = GrabProgress(grab_id=1, user_id=1, title="t")

    async def change_soon() -> None:
        await asyncio.sleep(0.05)
        progress.update(phase=GrabPhase.SCANNING)

    started = time.monotonic()
    changer = asyncio.create_task(change_soon())
    await progress.wait_newer(progress.version, 5)
    await changer
    assert progress.version == 2
    assert time.monotonic() - started < 1


async def test_waiting_times_out_without_a_change() -> None:
    progress = GrabProgress(grab_id=1, user_id=1, title="t")
    started = time.monotonic()
    await progress.wait_newer(progress.version, 0.1)
    assert 0.09 <= time.monotonic() - started < 1


def test_an_unchanged_update_does_not_bump_the_version() -> None:
    progress = GrabProgress(grab_id=1, user_id=1, title="t")
    assert progress.update(phase=GrabPhase.FINDING_TORRENT) is False
    assert progress.version == 1


def test_a_finished_grab_cannot_be_moved_again() -> None:
    progress = GrabProgress(grab_id=1, user_id=1, title="t")
    progress.update(phase=GrabPhase.READY, rating_key="5")
    assert progress.update(phase=GrabPhase.FAILED, error="late") is False
    assert progress.phase is GrabPhase.READY


async def tracked_row(
    app: FastAPI, db: AsyncSession, user: User, *, phase: str = "scanning",
    in_memory: bool = True, **columns: Any,
) -> tuple[Grab, GrabProgress | None]:
    """A grab row and, optionally, its live state, without running a task."""
    record = Grab(user_id=user.id, release_title=TITLE, release_guid="g", indexer_id=1,
                  tracking_phase=phase, **columns)
    db.add(record)
    await db.commit()
    progress = None
    if in_memory:
        progress = GrabProgress(grab_id=record.id, user_id=user.id, title=TITLE)
        app.state.cplus.grab_tracker._grabs[record.id] = progress
    return record, progress


async def registered_user(db: AsyncSession, token: str = PLEX_TOKEN, seerr_id: int = 42) -> User:
    user = User(seerr_user_id=seerr_id, plex_username=f"user{seerr_id}")
    db.add(user)
    await db.flush()
    await remember_token(db, token, user)
    await db.commit()
    return user


async def test_the_long_poll_answers_at_once_when_newer(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user)

    started = time.monotonic()
    response = await client.get(f"/grabs/{record.id}?since=0&wait=10", headers=plex_headers)

    assert time.monotonic() - started < 1
    assert response.json()["version"] == 1
    assert response.json()["phase"] == "finding_torrent"


async def test_the_long_poll_waits_for_a_change(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    record, progress = await tracked_row(app, db, user)
    assert progress is not None

    async def change_soon() -> None:
        await asyncio.sleep(0.1)
        progress.update(phase=GrabPhase.MATCHING)

    changer = asyncio.create_task(change_soon())
    started = time.monotonic()
    response = await client.get(f"/grabs/{record.id}?since=1&wait=10", headers=plex_headers)
    await changer

    elapsed = time.monotonic() - started
    assert 0.08 <= elapsed < 5
    assert response.json()["version"] == 2
    assert response.json()["phase"] == "matching"


async def test_the_long_poll_times_out_with_the_current_state(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user)

    started = time.monotonic()
    response = await client.get(f"/grabs/{record.id}?since=1&wait=0.2", headers=plex_headers)

    assert 0.18 <= time.monotonic() - started < 2
    assert response.status_code == 200
    assert response.json()["version"] == 1


async def test_the_long_poll_never_waits_longer_than_the_cap(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grabs_route, "MAX_WAIT_SECONDS", 0.1)
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user)

    started = time.monotonic()
    response = await client.get(f"/grabs/{record.id}?since=1&wait=600", headers=plex_headers)

    assert time.monotonic() - started < 2
    assert response.status_code == 200


async def test_the_long_poll_releases_the_database_while_waiting(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    """Another request that writes must not queue behind an open poll."""
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user)

    poll = asyncio.create_task(
        client.get(f"/grabs/{record.id}?since=1&wait=2", headers=plex_headers)
    )
    await asyncio.sleep(0.1)
    started = time.monotonic()
    other = await client.get(f"/grabs/{record.id}?since=0", headers=plex_headers)
    assert other.status_code == 200
    assert time.monotonic() - started < 1
    poll.cancel()


# --------------------------------------------------------------------------- #
# Auth and lookup
# --------------------------------------------------------------------------- #


async def test_grabs_need_a_registered_token(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user)

    assert (await client.get(f"/grabs/{record.id}")).status_code == 401
    unknown = await client.get(f"/grabs/{record.id}", headers={"X-Plex-Token": "never-registered"})
    assert unknown.status_code == 401


async def test_someone_elses_grab_is_not_found(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    await registered_user(db)
    other = await registered_user(db, token="other-token", seerr_id=43)
    live, _ = await tracked_row(app, db, other)
    stored, _ = await tracked_row(app, db, other, phase="ready", in_memory=False)

    assert (await client.get(f"/grabs/{live.id}", headers=plex_headers)).status_code == 404
    assert (await client.get(f"/grabs/{stored.id}", headers=plex_headers)).status_code == 404
    owner = {"X-Plex-Token": "other-token"}
    assert (await client.get(f"/grabs/{live.id}", headers=owner)).status_code == 200


async def test_an_unknown_or_untracked_grab_is_not_found(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    untracked = Grab(user_id=user.id, release_title=TITLE, release_guid="g", indexer_id=1)
    db.add(untracked)
    await db.commit()

    assert (await client.get("/grabs/98765", headers=plex_headers)).status_code == 404
    assert (await client.get(f"/grabs/{untracked.id}", headers=plex_headers)).status_code == 404


async def test_after_a_restart_a_finished_grab_answers_from_its_row(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    record, _ = await tracked_row(
        app, db, user, phase="ready", in_memory=False, rating_key="777", torrent_hash=HASH
    )

    body = (await client.get(f"/grabs/{record.id}", headers=plex_headers)).json()

    assert body["phase"] == "ready"
    assert body["rating_key"] == "777"


async def test_after_a_restart_an_unfinished_grab_reads_as_failed(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    user = await registered_user(db)
    record, _ = await tracked_row(app, db, user, phase="matching", in_memory=False)

    body = (await client.get(f"/grabs/{record.id}?since=5&wait=10", headers=plex_headers)).json()

    assert body["phase"] == "failed"
    assert body["error"] == grabs_route.RESTARTED_ERROR


# --------------------------------------------------------------------------- #
# Capability
# --------------------------------------------------------------------------- #


async def test_the_capability_is_on_only_when_tracking_can_work(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config
) -> None:
    assert (await client.get("/capabilities")).json()["grab_tracking"] is True

    tracking.plex_server_base_url = None
    await db.commit()
    assert (await client.get("/capabilities")).json()["grab_tracking"] is False

    tracking.plex_server_base_url = PLEX
    tracking.grab_tracking_enabled = False
    await db.commit()
    assert (await client.get("/capabilities")).json()["grab_tracking"] is False


def test_settings_are_none_until_everything_is_set(tracking_config: Config) -> None:
    assert TrackingSettings.from_config(tracking_config) is not None
    tracking_config.qbittorrent_url = None
    assert TrackingSettings.from_config(tracking_config) is None


@pytest.fixture
def tracking_config() -> Config:
    return Config(
        qbittorrent_url=QBT,
        grab_tracking_enabled=True,
        plex_admin_token=PLEX_ADMIN_TOKEN,
        plex_server_base_url=PLEX,
    )


# --------------------------------------------------------------------------- #
# Admin UI
# --------------------------------------------------------------------------- #


async def test_the_config_page_offers_qbittorrent_and_never_shows_the_password(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config
) -> None:
    await signed_in(client, db)
    page = (await client.get("/admin/config")).text

    assert "qBittorrent" in page
    assert QBT in page
    assert 'value="admin"' in page
    assert QBT_PASSWORD not in page
    assert "Follow grabs into Plex" in page


async def test_saving_qbittorrent_settings(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)
    await client.post(
        "/admin/config",
        data={"prowlarr_url": PROWLARR_URL, "qbittorrent_url": f"{QBT}/",
              "qbittorrent_username": " admin ", "qbittorrent_password": QBT_PASSWORD},
    )
    await db.refresh(configured)
    assert configured.qbittorrent_url == QBT
    assert configured.qbittorrent_username == "admin"
    assert configured.qbittorrent_password == QBT_PASSWORD

    # A blank password keeps the saved one; a post without the fields leaves them.
    await client.post(
        "/admin/config",
        data={"prowlarr_url": PROWLARR_URL, "qbittorrent_url": QBT,
              "qbittorrent_username": "admin", "qbittorrent_password": ""},
    )
    await client.post("/admin/config", data={"prowlarr_url": PROWLARR_URL})
    await db.refresh(configured)
    assert configured.qbittorrent_password == QBT_PASSWORD
    assert configured.qbittorrent_url == QBT

    # An emptied URL clears it.
    await client.post("/admin/config", data={"prowlarr_url": PROWLARR_URL, "qbittorrent_url": ""})
    await db.refresh(configured)
    assert configured.qbittorrent_url is None


@respx.mock
async def test_verify_qbittorrent_reports_the_version(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config
) -> None:
    FakeQbt().install()
    respx.get(f"{QBT}/api/v2/app/version").mock(return_value=httpx.Response(200, text="v5.0.1"))
    respx.get(f"{QBT}/api/v2/app/webapiVersion").mock(
        return_value=httpx.Response(200, text="2.11.2")
    )
    await signed_in(client, db)

    as_json = (await client.post("/admin/config/verify-qbittorrent")).json()
    as_html = (await client.post("/admin/config/verify-qbittorrent?format=html")).text

    assert as_json == {"ok": True, "message": "Connected to qBittorrent v5.0.1 (Web API 2.11.2)."}
    assert "v5.0.1" in as_html


@respx.mock
async def test_verify_qbittorrent_reports_a_refused_password(
    client: httpx.AsyncClient, db: AsyncSession, tracking: Config
) -> None:
    respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))
    await signed_in(client, db)

    result = (await client.post("/admin/config/verify-qbittorrent")).json()

    assert result["ok"] is False
    assert "username or password" in result["message"]
    assert QBT_PASSWORD not in result["message"]


async def test_verify_qbittorrent_before_configuring_is_not_a_crash(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    result = (await client.post("/admin/config/verify-qbittorrent")).json()
    assert result["ok"] is False


async def test_the_switch_saves_and_returns_itself(
    client: httpx.AsyncClient, db: AsyncSession, configured: Config
) -> None:
    await signed_in(client, db)

    on = await client.post("/admin/config/grab-tracking", data={"tracking_enabled": "on"})
    await db.refresh(configured)
    assert configured.grab_tracking_enabled is True
    assert "checked" in on.text
    assert "inactive until a qBittorrent URL is saved" in on.text

    off = await client.post("/admin/config/grab-tracking", data={})
    await db.refresh(configured)
    assert configured.grab_tracking_enabled is False
    assert "Off" in off.text


async def test_the_switch_and_verify_are_admin_only(client: httpx.AsyncClient) -> None:
    for path in ("/admin/config/grab-tracking", "/admin/config/verify-qbittorrent"):
        response = await client.post(path, follow_redirects=False)
        assert response.status_code == 303


async def test_the_grabs_page_shows_each_grabs_tracking_phase(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    admin = await signed_in(client, db)
    await tracked_row(app, db, admin, phase="ready", in_memory=False, rating_key="1")
    await tracked_row(app, db, admin, phase="failed", in_memory=False,
                      tracking_error="Could not reach qBittorrent.")
    await tracked_row(app, db, admin, phase="matching", in_memory=True)

    page = (await client.get("/admin/grabs")).text

    assert "in Plex" in page
    assert 'title="Could not reach qBittorrent."' in page
    assert "finding torrent" in page  # the live state, not the row


# --------------------------------------------------------------------------- #
# Production logging
# --------------------------------------------------------------------------- #


def test_the_entrypoint_gives_tracking_loggers_a_handler_at_the_configured_level() -> None:
    """uvicorn configures only its own loggers; without this, INFO lines vanish."""
    from cplus_service.__main__ import VERBOSE_LOGGERS, configure_logging

    saved = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).level,
               logging.getLogger(name).propagate)
        for name in VERBOSE_LOGGERS
    }
    try:
        configure_logging("info")
        for name in VERBOSE_LOGGERS:
            target = logging.getLogger(name)
            assert target.level == logging.INFO
            assert target.handlers
        assert logging.getLogger("cplus_service.tracking.runner").isEnabledFor(logging.INFO)
        # Only these: the rest of the service is left as it was.
        assert logging.getLogger("cplus_service.api.app").getEffectiveLevel() == logging.WARNING
        configure_logging("trace")
        assert logging.getLogger(VERBOSE_LOGGERS[0]).level == logging.DEBUG
    finally:
        for name, (handlers, level, propagate) in saved.items():
            target = logging.getLogger(name)
            target.handlers = handlers
            target.setLevel(level)
            target.propagate = propagate
