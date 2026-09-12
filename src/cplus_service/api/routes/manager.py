"""The admin app's request-manager endpoints.

A different caller from both tvOS and the browser admin webui: it authenticates
with a Plex token like tvOS does, but — unlike tvOS — always validates live
against Seerr, because these operations (grabbing a specific release directly,
unrestricted search) have no action and no permission grant of their own to
check against the cache. Named ``/manager/*``
after that live check, to keep it visually distinct from tvOS's ``/grab`` and
``/titles/{imdb_id}/actions`` and from the cookie-authenticated ``/admin/*``
webui.

Every endpoint here gates on the ``MANAGE_REQUESTS`` bit, ``/tmdb-token``
included: the admin app needs TMDB to turn a request into the IMDB id it
searches on, so anyone who may manage requests needs it too.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from ...auth.identity import authenticate_plex_token
from ...db.models import ActivityLog, EventType
from ...search.categorize import categorize_releases
from ...search.stream import stream_search
from ...seerr.client import SeerrAuthError, SeerrError
from ..deps import (
    ConfigDep,
    DbDep,
    PlexTokenDep,
    ProwlarrDep,
    SeerrDep,
    StateDep,
    require_request_manager,
)
from ..grab_core import execute_grab
from ..schemas import GrabResponse, ManagerGrabRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manager", tags=["manager"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"


@router.post("/grab", response_model=GrabResponse)
async def grab(
    db: DbDep,
    state: StateDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    background: BackgroundTasks,
    body: ManagerGrabRequest,
) -> GrabResponse | JSONResponse:
    """Grab a release straight to Prowlarr's default client, no action involved."""
    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            exc.detail or "Seerr rejected this Plex token",
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    return await execute_grab(
        db,
        prowlarr,
        user=user,
        action=None,
        # Not the caller's to choose: which client a grab lands in is set per
        # action on the Actions page, and an action-free grab has no such
        # setting to read, so Prowlarr picks its own default.
        download_client_id=None,
        body=body,
        state=state,
        background=background,
    )


@router.get("/search")
async def search(
    db: DbDep,
    config: ConfigDep,
    prowlarr: ProwlarrDep,
    seerr: SeerrDep,
    plex_token: PlexTokenDep,
    imdb_id: str | None = Query(default=None, min_length=1),
    query: str | None = Query(default=None, min_length=1),
    preferred_only: bool = Query(default=False),
) -> StreamingResponse:
    """Unrestricted Prowlarr search for the admin app: by IMDB id or free text.

    Exactly one of ``imdb_id`` or ``query`` must be given. Never scored — there
    is no action here to score against, and picking a release to grab directly
    (``POST /manager/grab``) doesn't need one; instead, every result is
    categorised, sorted and tagged by
    :func:`~cplus_service.search.categorize.categorize_releases` — this is the
    one search path where cplus does that itself rather than leaving it to the
    client. See that module for the category list and sort/tag rules.

    This is the *only* way to search Prowlarr independent of holding an
    action — regular tvOS users only ever see Prowlarr results through an
    action they hold, at ``GET /titles/{imdb_id}/actions``, which is exactly
    the access control this endpoint would bypass for anyone. Restricted to
    callers who can manage requests and checked against Seerr live, same gate
    as every other ``/manager/*`` endpoint.
    """
    if (imdb_id is None) == (query is None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of imdb_id or query.",
        )

    try:
        user, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.ADMIN,
            detail={
                "kind": "search",
                "imdb_id": imdb_id,
                "query": query,
                "preferred_only": preferred_only,
                "preferred_indexer_id": config.preferred_indexer_id,
            },
        )
    )

    preferred_indexer_id = config.preferred_indexer_id

    # End the transaction before the stream starts. The request dependency's
    # own commit does not run until the body drains, and a Prowlarr search can
    # take a minute — holding a SQLite write lock that long fails every
    # concurrent request with ``database is locked``, which is what made the
    # admin app report "cplus-server ran into a problem" on a second screen
    # while a search was still running. See :func:`..deps.get_db`.
    await db.commit()

    async def body() -> AsyncIterator[str]:
        # `stream_search`'s `all` phase carries only what the `preferred` phase
        # had not already sent (see its module docstring) — a delta, not the
        # full set. Accumulating here before categorising is what turns that
        # back into "every result seen so far", so the admin app never has to
        # merge releases across lines itself; it just renders the categories
        # it was handed.
        accumulated: list[Any] = []
        async for phase in stream_search(
            prowlarr=prowlarr,
            imdb_id=imdb_id,
            query=query,
            preferred_only=preferred_only,
            actions=[],
            preferred_indexer_id=preferred_indexer_id,
        ):
            accumulated.extend(phase.releases)
            payload: dict[str, Any] = {
                "phase": phase.phase,
                "categories": categorize_releases(accumulated),
            }
            if phase.error is not None:
                payload["error"] = phase.error
            yield json.dumps(payload, separators=(",", ":")) + "\n"

    return StreamingResponse(
        body(),
        media_type=NDJSON_MEDIA_TYPE,
        # Proxies love to buffer streamed responses; this asks nginx not to.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/tmdb-token")
async def tmdb_token(
    db: DbDep, config: ConfigDep, seerr: SeerrDep, plex_token: PlexTokenDep
) -> Any:
    """The saved TMDB bearer token, verbatim. **Request managers and up.**

    This is a deliberate exception to how every other secret in this service
    is handled: the Prowlarr key never leaves the server, and the Seerr admin
    key is never even stored (see ``/seerr/*``). Handing this one back over
    the API trades that same discipline for convenience — it's a low-impact,
    easily rotated key with no access to this service's own data.

    Gated on ``MANAGE_REQUESTS``, the same bit as every other ``/manager/*``
    endpoint, because that is who needs it: the admin app resolves a request's
    TMDB id to an IMDB id before it can search for releases, so withholding
    the token from a request manager takes away the search they are entitled
    to run — a real loss of function to protect a key that is already handed
    to the admin standing next to them.
    """
    try:
        _, auth = await authenticate_plex_token(db, seerr, plex_token)
    except SeerrAuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, exc.detail or "Seerr rejected this Plex token"
        ) from exc
    except SeerrError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach Seerr: {exc}"
        ) from exc

    require_request_manager(auth)

    return {"tmdb_bearer_token": config.tmdb_bearer_token}
