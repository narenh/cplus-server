"""Seerr client tests.

The cookie-isolation tests here guard a real bug that was caught during
development: httpx's normal request path merges the client's cookie jar into
every outgoing request, and Seerr sets ``connect.sid`` on each ``/auth/plex``
response. On a shared client that meant user A's Seerr session was attached to
user B's request — filing B's request under A's account. Both directions of that
leak are pinned below.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cplus_service.seerr.client import SeerrAuthError, SeerrClient, SeerrError
from cplus_service.seerr.models import SeerrPermission

BASE_URL = "http://seerr.test:5055"


def user_payload(**overrides) -> dict:  # noqa: ANN003
    return {"id": 42, "permissions": 32, "plexUsername": "someone", **overrides}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@respx.mock
async def test_authenticate_posts_the_plex_token() -> None:
    route = respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=user_payload())
    )

    async with SeerrClient(BASE_URL) as seerr:
        auth = await seerr.authenticate_plex("tok")

    assert auth.user.id == 42
    assert auth.user.plex_username == "someone"
    assert route.calls[0].request.content == b'{"authToken":"tok"}'


@respx.mock
async def test_authenticate_captures_the_session_cookie() -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200,
            json=user_payload(),
            headers={"set-cookie": "connect.sid=abc123; Path=/; HttpOnly"},
        )
    )

    async with SeerrClient(BASE_URL) as seerr:
        auth = await seerr.authenticate_plex("tok")

    assert auth.session_cookie == "abc123"


@respx.mock
@pytest.mark.parametrize("code", [401, 403])
async def test_a_rejected_token_raises_auth_error(code: int) -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(code, json={"message": "no"})
    )

    async with SeerrClient(BASE_URL) as seerr:
        with pytest.raises(SeerrAuthError):
            await seerr.authenticate_plex("tok")


@respx.mock
async def test_an_unreachable_seerr_is_not_an_auth_error() -> None:
    # The distinction matters: callers turn SeerrAuthError into 401 and plain
    # SeerrError into 502, and a 401 would make a client bin a good token.
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        side_effect=httpx.ConnectError("refused")
    )

    async with SeerrClient(BASE_URL) as seerr:
        with pytest.raises(SeerrError) as excinfo:
            await seerr.authenticate_plex("tok")

    assert not isinstance(excinfo.value, SeerrAuthError)


@respx.mock
async def test_admin_is_read_from_the_bitmask() -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200, json=user_payload(id=7, permissions=SeerrPermission.ADMIN | 32)
        )
    )

    async with SeerrClient(BASE_URL) as seerr:
        auth = await seerr.authenticate_plex("tok")

    assert auth.user.is_admin is True


@respx.mock
async def test_a_plain_user_is_not_admin_even_as_id_one() -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=user_payload(id=1, permissions=32))
    )

    async with SeerrClient(BASE_URL) as seerr:
        auth = await seerr.authenticate_plex("tok")

    assert auth.user.is_admin is False


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


@respx.mock
async def test_create_movie_request_sends_no_seasons() -> None:
    route = respx.post(f"{BASE_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 5})
    )

    async with SeerrClient(BASE_URL) as seerr:
        result = await seerr.create_request(
            session_cookie="abc", media_type="movie", tmdb_id=693134
        )

    assert result.id == 5
    assert b'"seasons"' not in route.calls[0].request.content


@respx.mock
async def test_create_tv_request_passes_seasons_verbatim() -> None:
    route = respx.post(f"{BASE_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 6})
    )

    async with SeerrClient(BASE_URL) as seerr:
        await seerr.create_request(
            session_cookie="abc", media_type="tv", tmdb_id=1399, seasons=[0, 1]
        )

    body = json.loads(route.calls[0].request.content)
    # 0 is specials. We never substitute the literal "all", which excludes them.
    assert body == {"mediaType": "tv", "mediaId": 1399, "seasons": [0, 1]}


@respx.mock
async def test_a_rejection_carries_seerrs_own_message() -> None:
    respx.post(f"{BASE_URL}/api/v1/request").mock(
        return_value=httpx.Response(403, json={"message": "Request quota exceeded"})
    )

    async with SeerrClient(BASE_URL) as seerr:
        with pytest.raises(SeerrError) as excinfo:
            await seerr.create_request(
                session_cookie="abc", media_type="movie", tmdb_id=1
            )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Request quota exceeded"


# --------------------------------------------------------------------------- #
# Cookie isolation
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_session_cookie_from_one_user_never_reaches_another_request() -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200,
            json=user_payload(id=1, plexUsername="alice"),
            headers={"set-cookie": "connect.sid=ALICE-SESSION; Path=/"},
        )
    )
    route = respx.post(f"{BASE_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    async with httpx.AsyncClient() as shared:
        seerr = SeerrClient(BASE_URL, client=shared)
        # Alice authenticates, which sets connect.sid on the shared client's jar.
        await seerr.authenticate_plex("alice-token")
        # Bob then files a request with his own session.
        await seerr.create_request(
            session_cookie="BOB-SESSION", media_type="movie", tmdb_id=1
        )

    cookie_header = route.calls[0].request.headers.get("cookie", "")
    assert "BOB-SESSION" in cookie_header
    assert "ALICE-SESSION" not in cookie_header


@respx.mock
async def test_a_request_without_a_session_sends_no_cookie_at_all() -> None:
    respx.post(f"{BASE_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(
            200,
            json=user_payload(),
            headers={"set-cookie": "connect.sid=LEFTOVER; Path=/"},
        )
    )
    route = respx.post(f"{BASE_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    async with httpx.AsyncClient() as shared:
        seerr = SeerrClient(BASE_URL, client=shared)
        await seerr.authenticate_plex("tok")
        await seerr.create_request(session_cookie=None, media_type="movie", tmdb_id=1)

    assert "cookie" not in route.calls[0].request.headers


async def test_seerr_and_prowlarr_do_not_share_an_http_client() -> None:
    """A Seerr session cookie must never be sent to Prowlarr, a third party."""
    from pathlib import Path

    from asgi_lifespan import LifespanManager

    from cplus_service.api.app import create_app
    from cplus_service.db.session import create_engine

    engine = create_engine(Path(":memory:"))
    app = create_app(engine=engine, create_schema=True)
    async with LifespanManager(app):
        state = app.state.cplus
        assert state.http is not state.seerr_http
    await engine.dispose()
