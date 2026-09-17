"""Radarr client tests — HTTP mocked with respx, no live Radarr needed."""

from __future__ import annotations

import httpx
import pytest
import respx

from cplus_service.radarr.client import RadarrClient, RadarrError

BASE_URL = "http://radarr.test:7878"
API_KEY = "secret-key"


def client() -> RadarrClient:
    return RadarrClient(BASE_URL, API_KEY)


@respx.mock
async def test_verify_connection_returns_system_status() -> None:
    route = respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        return_value=httpx.Response(
            200, json={"version": "5.14.0", "appName": "Radarr", "instanceName": "Movies"}
        )
    )

    async with client() as radarr:
        status = await radarr.verify_connection()

    assert status.version == "5.14.0"
    assert status.app_name == "Radarr"
    assert status.instance_name == "Movies"
    assert route.calls[0].request.headers["X-Api-Key"] == API_KEY


async def test_a_trailing_slash_on_the_base_url_is_normalised() -> None:
    assert RadarrClient(f"{BASE_URL}/", API_KEY).base_url == BASE_URL


@respx.mock
async def test_bad_api_key_raises_with_the_status_code() -> None:
    respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    async with client() as radarr:
        with pytest.raises(RadarrError) as excinfo:
            await radarr.verify_connection()

    assert excinfo.value.status_code == 401
    # The diagnostic message quotes Radarr; the summary keeps the host out of it.
    assert "Unauthorized" in str(excinfo.value)
    assert BASE_URL not in excinfo.value.summary


@respx.mock
async def test_transport_failure_is_wrapped() -> None:
    respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with client() as radarr:
        with pytest.raises(RadarrError) as excinfo:
            await radarr.verify_connection()

    assert excinfo.value.status_code is None


@respx.mock
async def test_a_non_json_body_is_wrapped() -> None:
    respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        return_value=httpx.Response(200, text="<html>not radarr</html>")
    )

    async with client() as radarr:
        with pytest.raises(RadarrError):
            await radarr.verify_connection()


@respx.mock
async def test_a_json_array_is_not_mistaken_for_a_working_connection() -> None:
    # A URL pointing at something else can answer 200 with a shape that would
    # validate into an empty status and read as success.
    respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with client() as radarr:
        with pytest.raises(RadarrError):
            await radarr.verify_connection()


@respx.mock
async def test_an_externally_owned_http_client_is_not_closed() -> None:
    respx.get(f"{BASE_URL}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "5.14.0"})
    )

    async with httpx.AsyncClient() as http:
        radarr = RadarrClient(BASE_URL, API_KEY, client=http)
        await radarr.verify_connection()
        await radarr.aclose()
        assert not http.is_closed
