"""``GET /search`` — the streamed, action-agnostic search.

All database work happens before the first byte is written, so nothing touches
the request-scoped session from inside the streaming generator (where its
lifetime is no longer guaranteed). The generator only needs the Prowlarr client,
the loaded actions, and the process-wide caches.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Action, ActivityLog, EventType, Permission, QualityProfile
from ...quality.models import QualityProfile as ProfileSchema
from ...search.stream import ScorableAction, stream_search
from ..deps import CachedUserDep, ConfigDep, DbDep, ProwlarrDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"


async def scorable_actions(session: AsyncSession, user_id: int) -> list[ScorableAction]:
    """The user's actions that can actually produce a recommendation.

    The built-in Request action is excluded: it has no quality profile and never
    touches Prowlarr, so there is nothing to score it against.
    """
    result = await session.execute(
        select(Action, QualityProfile)
        .join(Permission, Permission.action_id == Action.id)
        .join(QualityProfile, QualityProfile.id == Action.quality_profile_id)
        .where(Permission.user_id == user_id, Action.is_system.is_(False))
        .order_by(Action.id)
    )
    return [
        ScorableAction(
            id=action.id,
            name=action.name,
            profile=ProfileSchema(id=profile.id, name=profile.name, rules=profile.rules),
        )
        for action, profile in result.all()
    ]


@router.get("/search")
async def search(
    state: StateDep,
    db: DbDep,
    config: ConfigDep,
    prowlarr: ProwlarrDep,
    user: CachedUserDep,
    imdb_id: str | None = Query(default=None, min_length=1),
    query: str | None = Query(default=None, min_length=1),
    preferred_only: bool = Query(default=False),
    type: Literal["movie"] = Query(default="movie"),
) -> StreamingResponse:
    """Search Prowlarr and stream results as NDJSON.

    Two modes, exactly one of which must be given:

    * ``imdb_id`` — the movie search. Category-scoped, unambiguous, and each of
      the caller's actions gets a recommendation scored against its quality
      profile.
    * ``query`` — free text the user typed. Not category-scoped, so TV and
      anything else Prowlarr indexes can come back, and **no recommendation is
      attempted**: ``recommendations`` is always ``{}`` and every quality
      profile is ignored.

    ``preferred_only`` restricts the search to the admin's preferred indexer.
    It defaults to false — all indexers. With no preferred indexer configured it
    is a no-op rather than an error.

    Authenticated from the stored Plex-token mapping only — no outbound Plex or
    Seerr call, which is what keeps this fast.

    The response is one JSON object per line. See
    :mod:`cplus_service.search.stream` for the phase semantics; the short
    version for a client is: **apply the last line you received, wholesale**,
    union the ``releases`` arrays by guid, and treat ``phase: "all"`` as the
    end of the stream.
    """
    if (imdb_id is None) == (query is None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of imdb_id or query.",
        )
    if type != "movie":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only type=movie is supported; this service is movies-only outside of"
            " /request and free-text query.",
        )

    # A text query is never scored, so there is no reason to load the profiles.
    actions = [] if query is not None else await scorable_actions(db, user.id)

    # Logged up front rather than after the stream drains, so a client that
    # disconnects mid-search still leaves an audit trail.
    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={
                "imdb_id": imdb_id,
                "query": query,
                "preferred_only": preferred_only,
                "action_ids": [action.id for action in actions],
                "preferred_indexer_id": config.preferred_indexer_id,
            },
        )
    )

    preferred_indexer_id = config.preferred_indexer_id

    async def body() -> AsyncIterator[str]:
        async for phase in stream_search(
            prowlarr=prowlarr,
            imdb_id=imdb_id,
            query=query,
            preferred_only=preferred_only,
            actions=actions,
            preferred_indexer_id=preferred_indexer_id,
        ):
            yield phase.to_ndjson_line()

    return StreamingResponse(
        body(),
        media_type=NDJSON_MEDIA_TYPE,
        # Proxies love to buffer streamed responses; this asks nginx not to.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
