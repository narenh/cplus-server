"""``/movies/tmdb/{id}/actions`` and ``/tv/tmdb/{id}/actions``.

The two reduced-answer endpoints, for titles the movie search path cannot serve:
a show, or a film whose metadata carries no IMDB id. What they must guarantee is
narrow — report Request when the caller holds it, report nothing else, and never
touch Prowlarr. The absence of that outbound call is itself the promise, so it is
asserted rather than assumed.

Both behave identically today and are tested identically, which is deliberate:
the split exists because TMDB ids are namespaced by media type, and because TV is
the one expected to diverge later. Parametrising over both is what will catch it
if only one of them changes by accident.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.bootstrap import REQUEST_ACTION_NAME
from cplus_service.db.models import Action, Config, Permission, User
from cplus_service.db.session import get_config

from .conftest import PROWLARR_URL, SEERR_URL, make_action, seerr_user_payload

TMDB_ID = 1396

#: Movie 550 and TV series 550 are unrelated titles, which is why these are two
#: paths rather than one.
ROUTES = ["/movies/tmdb", "/tv/tmdb"]


@pytest.fixture(params=ROUTES)
def route(request: pytest.FixtureRequest) -> str:
    return request.param


def actions_url(route: str, tmdb_id: int = TMDB_ID) -> str:
    return f"{route}/{tmdb_id}/actions"


async def signed_in(client: httpx.AsyncClient, headers: dict) -> None:
    respx.post(f"{SEERR_URL}/api/v1/auth/plex").mock(
        return_value=httpx.Response(200, json=seerr_user_payload())
    )
    assert (await client.get("/register", headers=headers)).status_code == 200


async def only_user(db: AsyncSession) -> User:
    return (await db.execute(select(User))).scalars().one()


async def request_action(db: AsyncSession) -> Action:
    result = await db.execute(select(Action).where(Action.is_system.is_(True)))
    return result.scalars().one()


async def grant(db: AsyncSession, user: User, action: Action) -> None:
    db.add(Permission(user_id=user.id, action_id=action.id))
    await db.commit()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


async def test_401s_without_a_token(client: httpx.AsyncClient, route: str) -> None:
    assert (await client.get(actions_url(route))).status_code == 401


async def test_401s_for_a_token_that_never_registered(
    client: httpx.AsyncClient, plex_headers: dict, route: str
) -> None:
    # Cache-only auth, like the movie endpoint: an unknown token is simply a miss.
    assert (await client.get(actions_url(route), headers=plex_headers)).status_code == 401


# --------------------------------------------------------------------------- #
# What they report
# --------------------------------------------------------------------------- #


@respx.mock
async def test_reports_request_when_the_caller_holds_it(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, route: str
) -> None:
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await request_action(db))

    response = await client.get(actions_url(route), headers=plex_headers)

    assert response.status_code == 200
    action = response.json()["actions"][0]
    assert action["kind"] == "request"
    assert action["name"] == REQUEST_ACTION_NAME
    assert action["recommended_release_guid"] is None


@respx.mock
async def test_reports_nothing_when_the_caller_holds_no_request_action(
    client: httpx.AsyncClient, plex_headers: dict, route: str
) -> None:
    await signed_in(client, plex_headers)

    response = await client.get(actions_url(route), headers=plex_headers)

    assert response.status_code == 200
    assert response.json()["actions"] == []


@respx.mock
async def test_a_prowlarr_backed_action_is_never_reported(
    client: httpx.AsyncClient,
    db: AsyncSession,
    plex_headers: dict,
    configured: Config,
    route: str,
) -> None:
    """Holding a grab action does not make one appear here.

    Reporting one would promise a recommendation these endpoints have no way to
    produce, and the client would draw a button that cannot work.
    """
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await make_action(db, "Add in 4K"))

    response = await client.get(actions_url(route), headers=plex_headers)

    assert response.json()["actions"] == []


@respx.mock
async def test_the_shape_matches_one_line_of_the_movie_stream(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, route: str
) -> None:
    # So a client decodes all three endpoints with one type and draws them with
    # one piece of code. `phase` and `releases` are inert, kept only for that.
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await request_action(db))

    body = (await client.get(actions_url(route), headers=plex_headers)).json()

    assert set(body) == {"phase", "releases", "actions"}
    assert body["phase"] == "all"
    assert body["releases"] == []
    assert set(body["actions"][0]) == {
        "id",
        "name",
        "display_title",
        "kind",
        "icon",
        "recommended_release_guid",
    }


@respx.mock
async def test_display_title_falls_back_to_the_name(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, route: str
) -> None:
    await signed_in(client, plex_headers)
    action = await request_action(db)
    await grant(db, await only_user(db), action)

    body = (await client.get(actions_url(route), headers=plex_headers)).json()

    assert body["actions"][0]["display_title"] == action.name


@respx.mock
async def test_an_admins_button_copy_is_used_when_set(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, route: str
) -> None:
    await signed_in(client, plex_headers)
    action = await request_action(db)
    action.display_title = "Ask for this"
    await db.commit()
    await grant(db, await only_user(db), action)

    body = (await client.get(actions_url(route), headers=plex_headers)).json()

    assert body["actions"][0]["display_title"] == "Ask for this"
    assert body["actions"][0]["name"] == REQUEST_ACTION_NAME


# --------------------------------------------------------------------------- #
# What they must not do
# --------------------------------------------------------------------------- #


@respx.mock
async def test_never_calls_prowlarr(
    client: httpx.AsyncClient,
    db: AsyncSession,
    plex_headers: dict,
    configured: Config,
    route: str,
) -> None:
    """The whole reason these exist rather than reusing the movie endpoint.

    A movie-category search for a show is guaranteed useless, and an IMDB-less
    film has no query to make at all — either would be an outbound search on
    every detail page a client opens, for nothing.
    """
    search = respx.get(url__startswith=f"{PROWLARR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    await signed_in(client, plex_headers)
    user = await only_user(db)
    await grant(db, user, await request_action(db))
    await grant(db, user, await make_action(db, "Add in 4K"))

    await client.get(actions_url(route), headers=plex_headers)

    assert not search.called


@respx.mock
async def test_answers_with_prowlarr_unconfigured(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict, route: str
) -> None:
    # The movie endpoint 503s without Prowlarr. Requesting has nothing to do with
    # Prowlarr, so an install that never configured it still answers here.
    assert (await get_config(db)).prowlarr_url is None
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await request_action(db))

    response = await client.get(actions_url(route), headers=plex_headers)

    assert response.status_code == 200
    assert len(response.json()["actions"]) == 1


@respx.mock
@pytest.mark.parametrize("tmdb_id", [1, 550, 999999999])
async def test_the_tmdb_id_is_not_consulted(
    client: httpx.AsyncClient,
    db: AsyncSession,
    plex_headers: dict,
    route: str,
    tmdb_id: int,
) -> None:
    # It is in the path because the id identifies the title, and because a route
    # that cannot name its subject is one nothing can later be added to.
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await request_action(db))

    response = await client.get(actions_url(route, tmdb_id), headers=plex_headers)

    assert response.status_code == 200
    assert len(response.json()["actions"]) == 1


@respx.mock
async def test_the_two_routes_are_distinct_and_both_registered(
    client: httpx.AsyncClient, db: AsyncSession, plex_headers: dict
) -> None:
    # Guards the reason for the split: the same id under each media type is two
    # different titles, and both paths must actually exist.
    await signed_in(client, plex_headers)
    await grant(db, await only_user(db), await request_action(db))

    movie = await client.get("/movies/tmdb/550/actions", headers=plex_headers)
    tv = await client.get("/tv/tmdb/550/actions", headers=plex_headers)

    assert movie.status_code == 200
    assert tv.status_code == 200
