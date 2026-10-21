"""The qBittorrent Web API client: signing in, signing in again, and the toggles.

qBittorrent is mocked with respx, answering the way its Web API v2 does: a 200
``Ok.`` with a ``SID`` cookie for a good login, a 200 ``Fails.`` for a bad one,
and a 403 for a request whose session has expired.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from cplus_service.qbittorrent.client import (
    QBittorrentAuthError,
    QBittorrentClient,
    QBittorrentError,
    Torrent,
)

QBT = "http://qbt.test:8080"
PASSWORD = "hunter2-secret"


def login_ok(sid: str = "sid-one") -> httpx.Response:
    return httpx.Response(
        200, text="Ok.", headers={"set-cookie": f"SID={sid}; HttpOnly; path=/; SameSite=Strict"}
    )


def torrent_json(**overrides: object) -> dict:
    return {
        "hash": "a" * 40,
        "name": "Some.Release.2024.1080p.WEB-DL-GRP",
        "added_on": 1_700_000_000,
        "progress": 0.25,
        "dlspeed": 5_000_000,
        "eta": 600,
        "state": "downloading",
        "seq_dl": False,
        "f_l_piece_prio": False,
        "save_path": "E:\\Movies\\",
        **overrides,
    }


@respx.mock
async def test_it_signs_in_once_and_sends_the_session_cookie() -> None:
    login = respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=login_ok())
    info = respx.get(f"{QBT}/api/v2/torrents/info").mock(
        return_value=httpx.Response(200, json=[torrent_json()])
    )

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        await qbt.torrents()
        await qbt.torrents()

    assert login.call_count == 1
    assert b"password=hunter2-secret" in login.calls[0].request.content
    assert info.call_count == 2
    assert all(call.request.headers["cookie"] == "SID=sid-one" for call in info.calls)


@respx.mock
async def test_an_expired_session_signs_in_again_and_retries_once() -> None:
    login = respx.post(f"{QBT}/api/v2/auth/login").mock(
        side_effect=[login_ok("sid-one"), login_ok("sid-two")]
    )
    info = respx.get(f"{QBT}/api/v2/torrents/info").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(403, text="Forbidden"),
            httpx.Response(200, json=[torrent_json()]),
        ]
    )

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        await qbt.torrents()
        torrents = await qbt.torrents()

    assert login.call_count == 2
    assert [t.name for t in torrents] == ["Some.Release.2024.1080p.WEB-DL-GRP"]
    assert info.calls[-1].request.headers["cookie"] == "SID=sid-two"


@respx.mock
async def test_a_wrong_password_is_an_auth_error_that_does_not_name_it() -> None:
    respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        with pytest.raises(QBittorrentAuthError) as caught:
            await qbt.app_version()

    assert "username or password" in str(caught.value)
    assert PASSWORD not in str(caught.value)


@respx.mock
async def test_a_page_that_is_not_qbittorrent_says_what_came_back() -> None:
    page = "<!DOCTYPE html><html><head><title>Sign in</title></head></html>"
    respx.post(f"{QBT}/api/v2/auth/login").mock(
        return_value=httpx.Response(
            200, text=page, headers={"content-type": "text/html; charset=utf-8"}
        )
    )

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        with pytest.raises(QBittorrentAuthError) as caught:
            await qbt.app_version()

    message = str(caught.value)
    assert "HTTP 200, text/html" in message and "<!DOCTYPE html>" in message
    assert "username or password" not in message
    assert PASSWORD not in message


@respx.mock
async def test_a_banned_address_says_so() -> None:
    respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=httpx.Response(403))

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        with pytest.raises(QBittorrentAuthError, match="banned"):
            await qbt.app_version()


@respx.mock
async def test_without_credentials_it_never_signs_in() -> None:
    """A Web UI that whitelists this address needs no login at all."""
    login = respx.post(f"{QBT}/api/v2/auth/login")
    respx.get(f"{QBT}/api/v2/app/version").mock(return_value=httpx.Response(200, text="v5.0.1"))

    async with QBittorrentClient(QBT) as qbt:
        assert await qbt.app_version() == "v5.0.1"

    assert not login.called


@respx.mock
async def test_a_refusal_without_credentials_explains_what_to_set() -> None:
    respx.get(f"{QBT}/api/v2/app/version").mock(return_value=httpx.Response(403))

    async with QBittorrentClient(QBT) as qbt:
        with pytest.raises(QBittorrentAuthError, match="username and password"):
            await qbt.app_version()


@respx.mock
async def test_an_unreachable_host_is_a_qbittorrent_error() -> None:
    respx.get(f"{QBT}/api/v2/app/version").mock(side_effect=httpx.ConnectError("refused"))

    async with QBittorrentClient(QBT) as qbt:
        with pytest.raises(QBittorrentError, match="ConnectError"):
            await qbt.app_version()


def test_the_password_never_appears_in_its_repr() -> None:
    assert PASSWORD not in repr(QBittorrentClient(QBT, "admin", PASSWORD))


@respx.mock
async def test_the_session_cookie_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(f"{QBT}/api/v2/auth/login").mock(return_value=login_ok("very-secret-sid"))
    respx.get(f"{QBT}/api/v2/torrents/info").mock(return_value=httpx.Response(200, json=[]))

    async with QBittorrentClient(QBT, "admin", PASSWORD) as qbt:
        await qbt.torrents()

    assert "very-secret-sid" not in caplog.text
    assert PASSWORD not in caplog.text


# --------------------------------------------------------------------------- #
# The toggles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seq_dl", "first_last", "expected"),
    [
        (False, False, ["sequential download", "first/last piece priority"]),
        (True, False, ["first/last piece priority"]),
        (False, True, ["sequential download"]),
        (True, True, []),
    ],
)
@respx.mock
async def test_the_toggles_only_flip_what_is_off(
    seq_dl: bool, first_last: bool, expected: list[str]
) -> None:
    """Both endpoints *toggle*: calling one on a setting already on switches it off."""
    sequential = respx.post(f"{QBT}/api/v2/torrents/toggleSequentialDownload").mock(
        return_value=httpx.Response(200)
    )
    piece_prio = respx.post(f"{QBT}/api/v2/torrents/toggleFirstLastPiecePrio").mock(
        return_value=httpx.Response(200)
    )
    torrent = Torrent.from_api(torrent_json(seq_dl=seq_dl, f_l_piece_prio=first_last))

    async with QBittorrentClient(QBT) as qbt:
        switched = await qbt.prioritise_start(torrent)

    assert switched == expected
    assert sequential.call_count == (0 if seq_dl else 1)
    assert piece_prio.call_count == (0 if first_last else 1)
    if sequential.called:
        assert sequential.calls[0].request.content == f"hashes={'a' * 40}".encode()


@respx.mock
async def test_it_reads_files_properties_and_piece_states() -> None:
    respx.get(f"{QBT}/api/v2/torrents/properties").mock(
        return_value=httpx.Response(
            200, json={"save_path": "E:\\Movies\\", "piece_size": 4194304, "pieces_num": 10}
        )
    )
    respx.get(f"{QBT}/api/v2/torrents/files").mock(
        return_value=httpx.Response(
            200,
            json=[{"index": 0, "name": "a.mkv", "size": 40_000_000, "progress": 0.1,
                   "piece_range": [0, 9]}],
        )
    )
    respx.get(f"{QBT}/api/v2/torrents/pieceStates").mock(
        return_value=httpx.Response(200, json=[2, 1, 0, 0])
    )

    async with QBittorrentClient(QBT) as qbt:
        props = await qbt.properties("a" * 40)
        files = await qbt.files("a" * 40)
        states = await qbt.piece_states("a" * 40)

    assert (props.save_path, props.piece_size, props.pieces_num) == ("E:\\Movies\\", 4194304, 10)
    assert files[0].piece_range == (0, 9)
    assert states == [2, 1, 0, 0]


def test_metadata_not_yet_known_reads_as_no_piece_size() -> None:
    from cplus_service.qbittorrent.client import TorrentProperties

    assert TorrentProperties.from_api({"piece_size": -1, "pieces_num": -1}).piece_size == 0


def test_a_torrent_answers_to_its_v1_and_v2_hashes() -> None:
    torrent = Torrent.from_api(
        torrent_json(hash="B" * 40, infohash_v1="b" * 40, infohash_v2="c" * 64)
    )
    assert torrent.hashes == {"b" * 40, "c" * 64}
