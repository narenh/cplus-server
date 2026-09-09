"""Plex PIN-flow client tests — HTTP mocked with respx, no live plex.tv needed.

This is the only place cplus-service talks to plex.tv, and it backs admin
sign-in, so the cases that matter are the ones the admin webui depends on: a PIN
that has not been claimed yet is a normal outcome rather than an error, and
every other failure arrives as :class:`PlexError` rather than an httpx exception
leaking into the route.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from cplus_service.plex.client import (
    PLEX_API,
    PRODUCT_NAME,
    PlexConnection,
    PlexError,
    PlexPinClient,
    PlexServerClient,
    PlexServerError,
    best_connection,
    discover_resources,
)

CLIENT_ID = "install-identifier-1234"


def client() -> PlexPinClient:
    return PlexPinClient(CLIENT_ID)


# --------------------------------------------------------------------------- #
# create_pin
# --------------------------------------------------------------------------- #


@respx.mock
async def test_create_pin_returns_the_id_and_code() -> None:
    route = respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 987, "code": "ABCD"})
    )

    async with client() as plex:
        pin_id, code = await plex.create_pin()

    assert (pin_id, code) == (987, "ABCD")

    request = route.calls[0].request
    # The identifier ties the two halves of one flow together, and keeps every
    # sign-in on the same registered device rather than adding a new one.
    assert request.headers["X-Plex-Client-Identifier"] == CLIENT_ID
    assert request.headers["X-Plex-Product"] == PRODUCT_NAME
    assert parse_qs(urlparse(str(request.url)).query)["strong"] == ["true"]


@respx.mock
async def test_a_pin_response_missing_its_code_is_an_error() -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(201, json={"id": 987})
    )

    async with client() as plex:
        with pytest.raises(PlexError, match="usable PIN"):
            await plex.create_pin()


@respx.mock
async def test_a_non_json_body_is_an_error_not_a_crash() -> None:
    respx.post(f"{PLEX_API}/pins").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )

    async with client() as plex:
        with pytest.raises(PlexError, match="non-JSON"):
            await plex.create_pin()


@respx.mock
async def test_an_http_error_carries_the_status_code() -> None:
    respx.post(f"{PLEX_API}/pins").mock(return_value=httpx.Response(429, text="slow down"))

    async with client() as plex:
        with pytest.raises(PlexError) as excinfo:
            await plex.create_pin()

    assert excinfo.value.status_code == 429


@respx.mock
async def test_a_transport_failure_is_wrapped() -> None:
    respx.post(f"{PLEX_API}/pins").mock(side_effect=httpx.ConnectError("no route"))

    async with client() as plex:
        with pytest.raises(PlexError) as excinfo:
            await plex.create_pin()

    # No status code: nothing ever answered.
    assert excinfo.value.status_code is None


# --------------------------------------------------------------------------- #
# check_pin
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_unclaimed_pin_is_none_rather_than_an_error() -> None:
    # The webui polls this on a timer; an admin who has not finished signing in
    # yet is the expected case, not a failure.
    respx.get(f"{PLEX_API}/pins/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "authToken": None})
    )

    async with client() as plex:
        assert await plex.check_pin(5) is None


@respx.mock
async def test_a_claimed_pin_returns_the_auth_token() -> None:
    respx.get(f"{PLEX_API}/pins/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "authToken": "plex-token-xyz"})
    )

    async with client() as plex:
        assert await plex.check_pin(5) == "plex-token-xyz"


@respx.mock
async def test_an_expired_pin_is_an_error() -> None:
    # plex.tv answers 404 once a PIN has expired.
    respx.get(f"{PLEX_API}/pins/5").mock(return_value=httpx.Response(404, text="gone"))

    async with client() as plex:
        with pytest.raises(PlexError) as excinfo:
            await plex.check_pin(5)

    assert excinfo.value.status_code == 404


# --------------------------------------------------------------------------- #
# auth_url
# --------------------------------------------------------------------------- #


def test_auth_url_carries_the_code_and_identifier() -> None:
    url = client().auth_url("ABCD")

    # The parameters live in the fragment, which is what app.plex.tv reads.
    fragment = urlparse(url).fragment.lstrip("?")
    params = parse_qs(fragment)
    assert params["code"] == ["ABCD"]
    assert params["clientID"] == [CLIENT_ID]
    assert "forwardUrl" not in params


def test_auth_url_includes_a_forward_url_when_given() -> None:
    url = client().auth_url("ABCD", forward_url="https://cplus.test/admin/config")

    params = parse_qs(urlparse(url).fragment.lstrip("?"))
    assert params["forwardUrl"] == ["https://cplus.test/admin/config"]


# --------------------------------------------------------------------------- #
# Client lifecycle
# --------------------------------------------------------------------------- #


async def test_an_injected_client_is_not_closed_by_aclose() -> None:
    # The app shares one long-lived httpx client across requests; a PIN client
    # closing it would break every later outbound call.
    shared = httpx.AsyncClient()
    plex = PlexPinClient(CLIENT_ID, client=shared)

    await plex.aclose()

    assert not shared.is_closed
    await shared.aclose()


async def test_an_owned_client_is_closed_by_aclose() -> None:
    plex = client()
    created = plex.client  # lazily constructed on first access

    await plex.aclose()

    assert created.is_closed


# --------------------------------------------------------------------------- #
# discover_resources
# --------------------------------------------------------------------------- #


def resource_payload(**overrides) -> dict:
    return {
        "name": "Naren's Server",
        "clientIdentifier": "abc123",
        "provides": "server",
        "owned": True,
        "accessToken": "server-token",
        "connections": [
            {
                "protocol": "https",
                "address": "1.2.3.4",
                "port": 32400,
                "uri": "https://1-2-3-4.plex.direct:32400",
                "local": False,
                "relay": False,
            }
        ],
        **overrides,
    }


@respx.mock
async def test_discover_resources_parses_servers_and_their_connections() -> None:
    respx.get(f"{PLEX_API}/resources").mock(
        return_value=httpx.Response(200, json=[resource_payload()])
    )

    async with httpx.AsyncClient() as http:
        resources = await discover_resources("plex-tok", CLIENT_ID, client=http)

    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == "Naren's Server"
    assert resource.client_identifier == "abc123"
    assert resource.owned is True
    assert resource.access_token == "server-token"
    assert resource.is_server is True
    assert resource.connections == (
        PlexConnection(
            protocol="https",
            address="1.2.3.4",
            port=32400,
            uri="https://1-2-3-4.plex.direct:32400",
            local=False,
            relay=False,
        ),
    )


@respx.mock
async def test_discover_resources_sends_the_token_and_identifying_headers() -> None:
    route = respx.get(f"{PLEX_API}/resources").mock(return_value=httpx.Response(200, json=[]))

    async with httpx.AsyncClient() as http:
        await discover_resources("plex-tok", CLIENT_ID, client=http)

    request = route.calls[0].request
    assert request.headers["X-Plex-Token"] == "plex-tok"
    assert request.headers["X-Plex-Client-Identifier"] == CLIENT_ID
    assert request.headers["X-Plex-Product"] == PRODUCT_NAME


@respx.mock
async def test_a_non_server_resource_is_not_is_server() -> None:
    respx.get(f"{PLEX_API}/resources").mock(
        return_value=httpx.Response(
            200, json=[resource_payload(provides="player,pubsub-player")]
        )
    )

    async with httpx.AsyncClient() as http:
        resources = await discover_resources("plex-tok", CLIENT_ID, client=http)

    assert resources[0].is_server is False


@respx.mock
async def test_discover_resources_ignores_non_dict_entries() -> None:
    respx.get(f"{PLEX_API}/resources").mock(
        return_value=httpx.Response(200, json=[resource_payload(), "garbage", None])
    )

    async with httpx.AsyncClient() as http:
        resources = await discover_resources("plex-tok", CLIENT_ID, client=http)

    assert len(resources) == 1


@respx.mock
async def test_discover_resources_error_status_raises() -> None:
    respx.get(f"{PLEX_API}/resources").mock(return_value=httpx.Response(401, text="nope"))

    async with httpx.AsyncClient() as http:
        with pytest.raises(PlexError) as excinfo:
            await discover_resources("plex-tok", CLIENT_ID, client=http)

    assert excinfo.value.status_code == 401


@respx.mock
async def test_discover_resources_non_json_is_an_error() -> None:
    respx.get(f"{PLEX_API}/resources").mock(return_value=httpx.Response(200, text="<html>"))

    async with httpx.AsyncClient() as http:
        with pytest.raises(PlexError, match="non-JSON"):
            await discover_resources("plex-tok", CLIENT_ID, client=http)


@respx.mock
async def test_discover_resources_a_transport_failure_is_wrapped() -> None:
    respx.get(f"{PLEX_API}/resources").mock(side_effect=httpx.ConnectError("no route"))

    async with httpx.AsyncClient() as http:
        with pytest.raises(PlexError):
            await discover_resources("plex-tok", CLIENT_ID, client=http)


@respx.mock
async def test_discover_resources_a_non_list_body_is_empty() -> None:
    respx.get(f"{PLEX_API}/resources").mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as http:
        assert await discover_resources("plex-tok", CLIENT_ID, client=http) == []


# --------------------------------------------------------------------------- #
# best_connection
# --------------------------------------------------------------------------- #


def connection(**overrides) -> PlexConnection:
    fields = {
        "protocol": "https",
        "address": "x",
        "port": 32400,
        "uri": "uri",
        "local": False,
        "relay": False,
        **overrides,
    }
    return PlexConnection(**fields)


def test_best_connection_is_none_for_an_empty_list() -> None:
    assert best_connection([]) is None


def test_best_connection_prefers_local_over_remote() -> None:
    local = connection(local=True, uri="local")
    remote = connection(local=False, uri="remote")

    assert best_connection([remote, local]) is local


def test_best_connection_prefers_non_relay_over_relay() -> None:
    direct = connection(relay=False, uri="direct")
    relayed = connection(relay=True, uri="relay")

    assert best_connection([relayed, direct]) is direct


def test_best_connection_prefers_non_relay_even_over_a_local_relay() -> None:
    # Relay status matters more than locality — a relay is always the slower,
    # third-party-routed path, whether or not plex.tv happens to call it local.
    local_relay = connection(local=True, relay=True, uri="local-relay")
    remote_direct = connection(local=False, relay=False, uri="remote-direct")

    assert best_connection([local_relay, remote_direct]) is remote_direct


# --------------------------------------------------------------------------- #
# PlexServerClient.list_library_sections
# --------------------------------------------------------------------------- #


SERVER_URL = "http://plex.local:32400"


def sections_payload(*directory: dict) -> dict:
    return {"MediaContainer": {"Directory": list(directory)}}


@respx.mock
async def test_list_library_sections_parses_the_directory() -> None:
    respx.get(f"{SERVER_URL}/library/sections").mock(
        return_value=httpx.Response(
            200,
            json=sections_payload(
                {"key": "1", "title": "Movies (4K HDR)", "type": "movie", "hidden": 0},
                {"key": "2", "title": "TV Shows", "type": "show", "hidden": 1},
            ),
        )
    )

    async with PlexServerClient(SERVER_URL, "server-tok") as plex:
        sections = await plex.list_library_sections()

    assert [(s.id, s.name, s.type, s.hidden) for s in sections] == [
        ("1", "Movies (4K HDR)", "movie", False),
        ("2", "TV Shows", "show", True),
    ]


@respx.mock
async def test_list_library_sections_sends_the_token() -> None:
    route = respx.get(f"{SERVER_URL}/library/sections").mock(
        return_value=httpx.Response(200, json=sections_payload())
    )

    async with PlexServerClient(SERVER_URL, "server-tok") as plex:
        await plex.list_library_sections()

    assert route.calls[0].request.headers["X-Plex-Token"] == "server-tok"


@respx.mock
async def test_list_library_sections_skips_entries_missing_required_fields() -> None:
    respx.get(f"{SERVER_URL}/library/sections").mock(
        return_value=httpx.Response(
            200,
            json=sections_payload(
                {"key": "1", "title": "Movies", "type": "movie", "hidden": 0},
                {"title": "No key", "type": "movie"},
                "garbage",
            ),
        )
    )

    async with PlexServerClient(SERVER_URL, "server-tok") as plex:
        sections = await plex.list_library_sections()

    assert [s.id for s in sections] == ["1"]


@respx.mock
async def test_list_library_sections_error_status_raises() -> None:
    respx.get(f"{SERVER_URL}/library/sections").mock(
        return_value=httpx.Response(401, text="nope")
    )

    async with PlexServerClient(SERVER_URL, "server-tok") as plex:
        with pytest.raises(PlexServerError) as excinfo:
            await plex.list_library_sections()

    assert excinfo.value.status_code == 401


@respx.mock
async def test_list_library_sections_a_transport_failure_is_wrapped() -> None:
    respx.get(f"{SERVER_URL}/library/sections").mock(side_effect=httpx.ConnectError("down"))

    async with PlexServerClient(SERVER_URL, "server-tok") as plex:
        with pytest.raises(PlexServerError):
            await plex.list_library_sections()
