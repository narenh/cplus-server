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
    imdb_id: str = Query(min_length=1),
    type: Literal["movie"] = Query(default="movie"),
) -> StreamingResponse:
    """Search for a movie and stream results as NDJSON.

    Authenticated from the Plex-token cache only — no outbound Plex or Seerr
    call, which is what keeps this fast.

    The response is one JSON object per line. See
    :mod:`cplus_service.search.stream` for the phase semantics; the short
    version for a client is: **apply the last line you received, wholesale**,
    and union the ``releases`` arrays by guid.
    """
    if type != "movie":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only type=movie is supported; this service is movies-only outside of /request.",
        )

    actions = await scorable_actions(db, user.id)

    # Logged up front rather than after the stream drains, so a client that
    # disconnects mid-search still leaves an audit trail.
    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={
                "imdb_id": imdb_id,
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
