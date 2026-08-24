"""``GET /search`` — free-text, action-agnostic search.

A string the user typed, not a title the client already knows about. Not
category-scoped, so TV and anything else Prowlarr indexes can come back, and
**no recommendation is attempted**: no quality profile can meaningfully rank
the results of an arbitrary string, so ``recommendations`` is always ``{}``
regardless of what the caller is permitted to do. Looking up a specific,
already-identified movie and getting back the caller's permitted actions each
with a recommended release is a different job, served by
``GET /titles/{imdb_id}/actions`` instead — see :mod:`cplus_service.api.routes.titles`.

All database work happens before the first byte is written, so nothing touches
the request-scoped session from inside the streaming generator (where its
lifetime is no longer guaranteed).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ...db.models import ActivityLog, EventType
from ...search.stream import stream_search
from ..deps import CachedUserDep, ConfigDep, DbDep, ProwlarrDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"


@router.get("/search")
async def search(
    db: DbDep,
    config: ConfigDep,
    prowlarr: ProwlarrDep,
    user: CachedUserDep,
    query: str = Query(min_length=1),
    preferred_only: bool = Query(default=False),
) -> StreamingResponse:
    """Search Prowlarr by free text and stream results as NDJSON.

    Not category-scoped, so TV and anything else Prowlarr indexes can come
    back, and no recommendation is attempted — ``recommendations`` is always
    ``{}`` and every quality profile is ignored. A single phase: with nothing
    to score, there is no reason to race a fast partial answer ahead of the
    full one.

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
    # Logged up front rather than after the stream drains, so a client that
    # disconnects mid-search still leaves an audit trail.
    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={
                "query": query,
                "preferred_only": preferred_only,
                "preferred_indexer_id": config.preferred_indexer_id,
            },
        )
    )

    preferred_indexer_id = config.preferred_indexer_id

    async def body() -> AsyncIterator[str]:
        async for phase in stream_search(
            prowlarr=prowlarr,
            query=query,
            preferred_only=preferred_only,
            actions=[],
            preferred_indexer_id=preferred_indexer_id,
        ):
            yield phase.to_ndjson_line()

    return StreamingResponse(
        body(),
        media_type=NDJSON_MEDIA_TYPE,
        # Proxies love to buffer streamed responses; this asks nginx not to.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
