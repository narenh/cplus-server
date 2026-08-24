"""``GET /titles/{imdb_id}/actions`` — the movie-detail-page call.

Triggered when tvOS opens a title's detail page, after ``GET /actions`` has
already run once at launch to establish the token mapping. For every action
the caller holds permission for, this reports what pressing that button would
do:

* the built-in Request action needs nothing else and is reported immediately —
  it has no quality profile and never touches Prowlarr;
* every Prowlarr-backed action is scored against its own quality profile, from
  one shared Prowlarr fetch, and reports its recommended release (or ``null``
  if nothing survived its filters).

The full candidate list rides along in the same response, so pressing
"view all releases" needs no second call — it is a client-side reveal of
``releases``, not a fetch.

Cache-only auth, same as ``GET /search``: no outbound Plex or Seerr call. This
endpoint used to be ``GET /search?imdb_id=...``; free-text, action-agnostic
search — a different job, tied to no title and to no action — is what
``GET /search`` is exclusively for now.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Action, ActivityLog, EventType, Permission, QualityProfile
from ...quality.models import QualityProfile as ProfileSchema
from ...search.stream import ScorableAction, stream_search
from ..deps import CachedUserDep, ConfigDep, DbDep, ProwlarrDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["client"])

NDJSON_MEDIA_TYPE = "application/x-ndjson"

KIND_REQUEST = "request"
KIND_GRAB = "grab"


async def scorable_actions(session: AsyncSession, user_id: int) -> list[ScorableAction]:
    """The user's actions that can actually produce a recommendation.

    The built-in Request action is excluded: it has no quality profile and
    never touches Prowlarr, so there is nothing to score it against. It is
    reported separately — see :func:`permitted_request_action`.
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


async def permitted_request_action(session: AsyncSession, user_id: int) -> Action | None:
    """The built-in Request action, if the caller has been granted it."""
    result = await session.execute(
        select(Action)
        .join(Permission, Permission.action_id == Action.id)
        .where(Permission.user_id == user_id, Action.is_system.is_(True))
    )
    return result.scalars().first()


@router.get("/titles/{imdb_id}/actions")
async def title_actions(
    imdb_id: str,
    db: DbDep,
    config: ConfigDep,
    prowlarr: ProwlarrDep,
    user: CachedUserDep,
    preferred_only: bool = Query(default=False),
) -> StreamingResponse:
    """Stream this title's releases plus the caller's action offers, as NDJSON.

    One or two phases depending on whether a preferred indexer is configured —
    see :mod:`cplus_service.search.stream` for the full phase semantics, which
    this reuses unchanged. As with ``GET /search``, the client's merge rule is
    **apply the last line you received, wholesale**: each line's ``actions``
    array is complete on its own, not a delta from the previous one.

    ``preferred_only`` restricts the search to the admin's preferred indexer;
    see ``GET /search`` for its exact semantics, which are identical here.
    """
    scorable = await scorable_actions(db, user.id)
    request_action = await permitted_request_action(db, user.id)

    # Logged up front rather than after the stream drains, so a client that
    # disconnects mid-search still leaves an audit trail.
    db.add(
        ActivityLog(
            user_id=user.id,
            event_type=EventType.SEARCH,
            detail={
                "imdb_id": imdb_id,
                "preferred_only": preferred_only,
                "action_ids": [action.id for action in scorable]
                + ([request_action.id] if request_action is not None else []),
                "preferred_indexer_id": config.preferred_indexer_id,
            },
        )
    )

    preferred_indexer_id = config.preferred_indexer_id

    async def body() -> AsyncIterator[str]:
        async for phase in stream_search(
            prowlarr=prowlarr,
            imdb_id=imdb_id,
            preferred_only=preferred_only,
            actions=scorable,
            preferred_indexer_id=preferred_indexer_id,
        ):
            payload = phase.to_payload()
            recommendations = payload.pop("recommendations")

            actions: list[dict[str, object]] = []
            if request_action is not None:
                actions.append(
                    {
                        "id": request_action.id,
                        "name": request_action.name,
                        "kind": KIND_REQUEST,
                        "recommended_release_guid": None,
                    }
                )
            for action in scorable:
                actions.append(
                    {
                        "id": action.id,
                        "name": action.name,
                        "kind": KIND_GRAB,
                        "recommended_release_guid": recommendations.get(str(action.id)),
                    }
                )
            payload["actions"] = actions

            yield json.dumps(payload, separators=(",", ":")) + "\n"

    return StreamingResponse(
        body(),
        media_type=NDJSON_MEDIA_TYPE,
        # Proxies love to buffer streamed responses; this asks nginx not to.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
