"""The TMDB-keyed action endpoints — what a caller can do with a title we cannot
search for.

``GET /titles/{imdb_id}/actions`` answers the full question for a movie: it
searches Prowlarr and scores what comes back against every action the caller
holds. These two answer the reduced question for the cases where that is not
possible, and today the answer is always the same one — **the built-in Request
action, if the caller holds it, and nothing else.**

Two of them, not one, because **TMDB ids are namespaced by media type**: movie
550 and TV series 550 are unrelated titles, so a single ``/{tmdb_id}/actions``
would be ambiguous the moment anything here actually looked the id up. Nothing
does yet — see below — but a path that cannot express the difference would have
to be replaced rather than extended, and older clients would be stranded on it.
The split matches ``POST /request``, which has always taken ``type`` alongside
``tmdb_id`` for exactly this reason.

They exist for different reasons, which is why they stay separate rather than
becoming one handler with a parameter:

``GET /movies/tmdb/{tmdb_id}/actions``
    A movie whose metadata carries no IMDB id. Prowlarr search here is IMDB-keyed,
    so there is nothing to search *with* — but the title can still be requested,
    since Seerr is TMDB-keyed throughout. This will never grow grab actions,
    because the missing id is the thing preventing them.

``GET /tv/tmdb/{tmdb_id}/actions``
    A TV title. Grab actions for TV are deliberately undecided — seasons, packs
    and per-episode releases all want answering first — so only Request is
    reported. This one *may* grow, and when it does it will diverge from the
    movie route rather than both drifting through a shared parameter.

**Neither ever calls Prowlarr.** For TV a movie-category search is guaranteed
useless; for an IMDB-less movie there is no query to make. Either would mean an
outbound search on every detail page a client opens, for nothing.

A caller holding only Prowlarr-backed actions gets an empty list from both,
which is the honest answer: reporting a grab action would promise a
recommendation these endpoints have no way to produce, and the client would draw
a button that cannot work.

Responses are one object in the exact shape of a single line of the movie
endpoint's stream, so a client decodes all three with the same type and draws
them with the same code. Plain JSON rather than NDJSON because there is no
search to stream; ``phase`` and ``releases`` are inert and kept only for that
shape.

Cache-only auth, like the movie endpoint: no outbound Plex or Seerr call.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import User
from ...search.stream import PHASE_ALL
from ..deps import CachedUserDep, DbDep
from .titles import KIND_REQUEST, permitted_request_action

router = APIRouter(tags=["client"])


async def request_only_payload(db: AsyncSession, user: User) -> dict[str, Any]:
    """The shared body: Request if they hold it, nothing else, no releases."""
    request_action = await permitted_request_action(db, user.id)

    actions: list[dict[str, Any]] = []
    if request_action is not None:
        actions.append(
            {
                "id": request_action.id,
                "name": request_action.name,
                "display_title": request_action.button_title,
                "kind": KIND_REQUEST,
                "recommended_release_guid": None,
            }
        )

    return {"phase": PHASE_ALL, "releases": [], "actions": actions}


@router.get("/movies/tmdb/{tmdb_id}/actions")
async def movie_tmdb_actions(
    tmdb_id: int, db: DbDep, user: CachedUserDep
) -> dict[str, Any]:
    """What a caller can do with a movie that has no IMDB id.

    ``tmdb_id`` is not consulted — there is nothing to look it up for when the
    only offer is a request the client files itself. It is in the path because
    the id is what identifies the title, and because a route that cannot name
    its subject is one nothing else can ever be added to.
    """
    return await request_only_payload(db, user)


@router.get("/tv/tmdb/{tmdb_id}/actions")
async def tv_tmdb_actions(
    tmdb_id: int, db: DbDep, user: CachedUserDep
) -> dict[str, Any]:
    """What a caller can do with a TV title.

    Same body as the movie route today, and deliberately its own route: this is
    the one that may grow grab actions later, and it should be able to do so
    without touching the movie path.
    """
    return await request_only_payload(db, user)
