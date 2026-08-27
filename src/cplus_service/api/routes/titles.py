"""``GET /titles/{imdb_id}/actions`` — the movie-detail-page call.

Triggered when tvOS opens a title's detail page, after ``GET /register`` has
already run once at launch to establish the token mapping. For every action
the caller holds permission for, this reports what pressing that button would
do:

* the built-in Request action needs nothing else and is reported immediately —
  it has no quality profile and never touches Prowlarr;
* every Prowlarr-backed action is scored against its own quality profile, from
  one shared Prowlarr fetch, and reports its recommended release (or ``null``
  if nothing survived its filters).

Every action is reported with both a ``name`` and a ``display_title``, and the
client should print the **display title** on the button. The name is the
admin's own label — it identifies the action in the admin UI and in
notification text — while the display title is copy chosen for whoever is
holding the remote. An action with no display title configured reports its name
in both fields, so a client can read ``display_title`` unconditionally.

**Route on ``kind``, never on either of them.** Both are free text an admin can
change at any moment, the built-in Request action's included; ``kind`` is what
says whether pressing this button posts to ``/request`` or to ``/grab``.

The full candidate list rides along in the same response, so pressing
"view all releases" needs no second call — it is a client-side reveal of
``releases``, not a fetch.

**Holding a Prowlarr-backed action is what grants Prowlarr access at all.** A
caller with none — whether that's zero actions, or only the built-in Request
action, which never touches Prowlarr — never triggers a search: the response
is a single ``releases: []`` line naming whatever they *are* permitted (just
Request, or nothing). Actions are the only grant of indexer access a regular
user has; unrestricted search, independent of holding any action, is the admin
app's job at ``GET /manager/search`` instead.

Cache-only auth: no outbound Plex or Seerr call.
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
from ...search.stream import PHASE_ALL, ScorableAction, stream_search
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
            display_title=action.button_title,
            profile=ProfileSchema(
                id=profile.id,
                name=profile.name,
                rules=profile.rules,
                choices=profile.choices or [],
            ),
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
    this reuses unchanged. As with ``GET /manager/search``, the client's merge
    rule is **apply the last line you received, wholesale**: each line's
    ``actions`` array is complete on its own, not a delta from the previous
    one.

    ``preferred_only`` restricts the search to the admin's preferred indexer;
    see ``GET /manager/search`` for its exact semantics, which are identical
    here. It has no effect when the caller holds no Prowlarr-backed action,
    since no search happens at all in that case.
    """
    scorable = await scorable_actions(db, user.id)
    request_action = await permitted_request_action(db, user.id)

    request_offer: dict[str, object] | None = None
    if request_action is not None:
        request_offer = {
            "id": request_action.id,
            "name": request_action.name,
            "display_title": request_action.button_title,
            "kind": KIND_REQUEST,
            "recommended_release_guid": None,
        }

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
        if not scorable:
            # No Prowlarr-backed action held — actions are the only grant of
            # indexer access a regular user has, so there is nothing to search
            # for. (The Prowlarr dependency above still validated that it's
            # configured; it's just never called here.)
            payload = {
                "phase": PHASE_ALL,
                "releases": [],
                "actions": [request_offer] if request_offer is not None else [],
            }
            yield json.dumps(payload, separators=(",", ":")) + "\n"
            return

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
            if request_offer is not None:
                actions.append(request_offer)
            for action in scorable:
                actions.append(
                    {
                        "id": action.id,
                        "name": action.name,
                        "display_title": action.display_title or action.name,
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
