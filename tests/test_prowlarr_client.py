"""Prowlarr client tests — HTTP mocked with respx, no live Prowlarr needed."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cplus_service.prowlarr.client import ProwlarrClient, ProwlarrError

BASE_URL = "http://prowlarr.test:9696"
API_KEY = "secret-key"


def client() -> ProwlarrClient:
    return ProwlarrClient(BASE_URL, API_KEY)


@respx.mock
async def test_verify_connection_returns_system_status() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(
            200, json={"version": "1.28.0", "appName": "Prowlarr", "instanceName": "Home"}
        )
    )

    async with client() as prowlarr:
        status = await prowlarr.verify_connection()

    assert status.version == "1.28.0"
    assert status.app_name == "Prowlarr"
    assert route.calls[0].request.headers["X-Api-Key"] == API_KEY


@respx.mock
async def test_bad_api_key_raises_with_the_status_code() -> None:
    respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    async with client() as prowlarr:
        with pytest.raises(ProwlarrError) as excinfo:
            await prowlarr.verify_connection()

    assert excinfo.value.status_code == 401


@respx.mock
async def test_transport_failure_is_wrapped() -> None:
    respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with client() as prowlarr:
        with pytest.raises(ProwlarrError) as excinfo:
            await prowlarr.verify_connection()

    assert excinfo.value.status_code is None


@respx.mock
async def test_list_indexers() -> None:
    respx.get(f"{BASE_URL}/api/v1/indexer").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "Tracker One", "enable": True, "protocol": "torrent"},
                {"id": 2, "name": "Usenet Two", "enable": False, "protocol": "usenet"},
            ],
        )
    )

    async with client() as prowlarr:
        indexers = await prowlarr.list_indexers()

    assert [i.id for i in indexers] == [1, 2]
    assert indexers[0].name == "Tracker One"
    assert indexers[1].enable is False


@respx.mock
async def test_list_download_clients() -> None:
    respx.get(f"{BASE_URL}/api/v1/downloadclient").mock(
        return_value=httpx.Response(
            200, json=[{"id": 5, "name": "qBittorrent", "protocol": "torrent"}]
        )
    )

    async with client() as prowlarr:
        clients = await prowlarr.list_download_clients()

    assert clients[0].id == 5
    assert clients[0].name == "qBittorrent"


@respx.mock
async def test_search_movie_uses_the_imdb_token_and_movie_category() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with client() as prowlarr:
        await prowlarr.search_movie("tt0111161")

    params = route.calls[0].request.url.params
    assert params["query"] == "{ImdbId:0111161}"
    assert params["type"] == "movie"
    assert params["categories"] == "2000"


@respx.mock
async def test_search_movie_accepts_a_bare_imdb_id() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with client() as prowlarr:
        await prowlarr.search_movie("0111161")

    assert route.calls[0].request.url.params["query"] == "{ImdbId:0111161}"


@respx.mock
async def test_search_movie_can_be_scoped_to_specific_indexers() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with client() as prowlarr:
        await prowlarr.search_movie("tt0111161", indexer_ids=[3, 7])

    assert route.calls[0].request.url.params.get_list("indexerIds") == ["3", "7"]


@respx.mock
async def test_search_results_come_back_parsed_and_free_of_full_discs() -> None:
    respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "title": "Movie.2024.2160p.WEB-DL.DV.P8.HDR10+.DDP5.1.Atmos.HEVC-FLUX",
                    "guid": "guid-web",
                    "indexerId": 1,
                    "indexer": "Tracker One",
                    "size": 25_000_000_000,
                    "publishDate": "2024-05-01T12:00:00Z",
                    "seeders": 100,
                    "protocol": "torrent",
                },
                {
                    "title": "Movie.2024.COMPLETE.UHD.BLURAY-TERMiNAL",
                    "guid": "guid-disc",
                    "indexerId": 1,
                    "size": 80_000_000_000,
                },
            ],
        )
    )

    async with client() as prowlarr:
        releases = await prowlarr.search_movie("tt0111161")

    assert [r.guid for r in releases] == ["guid-web"]
    release = releases[0]
    assert release.dv_profile == 8
    assert release.is_hdr10plus is True
    assert release.has_atmos is True
    assert release.indexer == "Tracker One"
    assert release.size_bytes == 25_000_000_000
    assert release.is_full_disc is False


@respx.mock
async def test_grab_posts_guid_indexer_and_download_client() -> None:
    route = respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(201, json={"guid": "guid-web"})
    )

    async with client() as prowlarr:
        result = await prowlarr.grab(guid="guid-web", indexer_id=1, download_client_id=5)

    body = json.loads(route.calls[0].request.content)
    assert body == {"guid": "guid-web", "indexerId": 1, "downloadClientId": 5}
    assert result.guid == "guid-web"
    assert result.download_client_id == 5


@respx.mock
async def test_grab_failure_raises() -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(500, text="indexer exploded")
    )

    async with client() as prowlarr:
        with pytest.raises(ProwlarrError) as excinfo:
            await prowlarr.grab(guid="g", indexer_id=1, download_client_id=5)

    assert excinfo.value.status_code == 500


@respx.mock
async def test_an_externally_owned_http_client_is_not_closed() -> None:
    respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(200, json={"version": "1.0"})
    )

    async with httpx.AsyncClient() as shared:
        prowlarr = ProwlarrClient(BASE_URL, API_KEY, client=shared)
        await prowlarr.verify_connection()
        await prowlarr.aclose()
        assert shared.is_closed is False


@respx.mock
async def test_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(200, json={"version": "1.0"})
    )

    async with ProwlarrClient(f"{BASE_URL}/", API_KEY) as prowlarr:
        await prowlarr.verify_connection()

    assert route.called
