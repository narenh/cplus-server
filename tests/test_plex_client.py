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
    PlexError,
    PlexPinClient,
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
