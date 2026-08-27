"""Streamed search, in one or two phases.

Two modes, and they are shaped differently on purpose.

**IMDB search** is triggered by navigation in the client, so there is no query
ambiguity to resolve — the only problem is latency. Prowlarr fanning out to
every configured indexer is slow; the admin's preferred indexer alone is fast.
So we do both at once and stream the fast answer first, and each action gets a
recommendation scored against its quality profile.

**Free-text search** is a string the user typed. No quality profile can
meaningfully rank the results of an arbitrary string, so **no recommendation is
attempted and every profile is ignored** — ``recommendations`` is always ``{}``.
With no recommendation to race for, there is nothing to gain from splitting the
fetch, so a text query is a single phase. It is also not category-scoped: TV and
anything else Prowlarr indexes can come back.

``preferred_only`` collapses either mode to a single call scoped to the admin's
preferred indexer. If no preferred indexer is configured the flag is a no-op and
all indexers are searched, rather than an error.

**The last line is always ``phase: "all"``**, in every mode — that is the
client's signal that the stream is complete. ``preferred`` is an optional
earlier partial, never the last word.

In the two-phase case, both Prowlarr calls are issued concurrently:

1. scoped to ``config.preferred_indexer_id`` — **skipped entirely** when that is
   null ("All indexers"), because there is nothing distinct to fetch early;
2. across all indexers.

Phase 1 resolves first and yields a ``preferred`` line: the subset plus a
recommendation per action scored against it. Phase 2 yields an ``all`` line
carrying whatever phase 1 had not already sent.

**The ``all`` line always re-sends a recommendation for every action**, not just
the ones phase 1 left unresolved. The client's merge rule is therefore "take the
last line you received, wholesale" — no key-level merging, nothing to track, and
the same handling whether or not phase 1 ever ran. The re-sent values are
consistent with phase 1 rather than contradicting it: scoring runs through
:func:`~cplus_service.quality.engine.preferred_indexer_candidates`, so when the
preferred subset is non-empty the answer is unchanged, and when it is empty
every action falls back to the full set.

The ``all`` line is always emitted, including when a search yields nothing and
including when Prowlarr errors, so the client can always leave its loading
state. Because the response has already committed to 200 by then, a late failure
is reported in-band as an ``error`` field rather than as a status code.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..prowlarr.client import ProwlarrClient, ProwlarrError
from ..quality.engine import preferred_indexer_candidates, recommend
from ..quality.models import QualityProfile
from ..release.models import ParsedRelease

logger = logging.getLogger(__name__)

PHASE_PREFERRED = "preferred"
PHASE_ALL = "all"


@dataclass(frozen=True, slots=True)
class ScorableAction:
    """An action that can produce a recommendation.

    The built-in Request action never appears here — it has no quality profile
    and never touches Prowlarr, so it is not scorable at all.
    """

    id: int
    name: str
    profile: QualityProfile
    #: The button copy, already resolved — an action's ``display_title`` when
    #: it has one, otherwise its name. Carried alongside the name rather than
    #: replacing it because the two are reported separately on the wire.
    display_title: str = ""


@dataclass(frozen=True, slots=True)
class SearchPhase:
    """One NDJSON line."""

    phase: str
    releases: list[ParsedRelease] = field(default_factory=list)
    recommendations: dict[str, str | None] = field(default_factory=dict)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phase": self.phase,
            "releases": [release.model_dump(mode="json") for release in self.releases],
            "recommendations": self.recommendations,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    def to_ndjson_line(self) -> str:
        return json.dumps(self.to_payload(), separators=(",", ":")) + "\n"


def _recommendations(
    candidates: Sequence[ParsedRelease], actions: Sequence[ScorableAction]
) -> dict[str, str | None]:
    """Best release guid per action, or ``None`` where nothing survived the filters."""
    results: dict[str, str | None] = {}
    for action in actions:
        best = recommend(candidates, action.profile)
        results[str(action.id)] = best.guid if best is not None else None
    return results


async def stream_search(
    *,
    prowlarr: ProwlarrClient,
    actions: Sequence[ScorableAction],
    preferred_indexer_id: int | None,
    imdb_id: str | None = None,
    query: str | None = None,
    preferred_only: bool = False,
) -> AsyncIterator[SearchPhase]:
    """Yield the search phases in the order they resolve.

    Exactly one of ``imdb_id`` or ``query`` must be given. See the module
    docstring for how each mode is shaped.
    """
    if (imdb_id is None) == (query is None):
        raise ValueError("stream_search needs exactly one of imdb_id or query")

    text_mode = query is not None

    def fetch(indexer_ids: list[int] | None) -> Any:
        if query is not None:
            return prowlarr.search_query(query, indexer_ids=indexer_ids)
        return prowlarr.search_movie(str(imdb_id), indexer_ids=indexer_ids)

    # A free-text query is never scored. No quality profile can meaningfully
    # rank an arbitrary string's results — the user is browsing, not asking for
    # the best copy of a known film — so no recommendation is attempted at all
    # and every profile is ignored.
    scorable: Sequence[ScorableAction] = () if text_mode else actions

    # The preferred/all split exists to race a recommendation out early. With
    # nothing to score, and when the caller asked for one indexer only, there is
    # no second phase worth having.
    two_phase = not text_mode and not preferred_only and preferred_indexer_id is not None

    if not two_phase:
        # `preferred_only` with nothing configured is a no-op rather than an
        # error: the flag asks for the fast path, and searching everything is
        # the honest answer when there is no preferred indexer to be fast about.
        scope = [preferred_indexer_id] if preferred_only and preferred_indexer_id else None

        releases: list[ParsedRelease] = []
        error: str | None = None
        try:
            releases = await fetch(scope)
        except ProwlarrError as exc:
            logger.warning("search failed: %s", exc)
            error = str(exc)

        yield SearchPhase(
            phase=PHASE_ALL,
            releases=releases,
            recommendations=_recommendations(
                preferred_indexer_candidates(releases, preferred_indexer_id), scorable
            ),
            error=error,
        )
        return

    preferred_task: asyncio.Task[list[ParsedRelease]] | None = None
    if preferred_indexer_id is not None:
        preferred_task = asyncio.create_task(fetch([preferred_indexer_id]))
    all_task = asyncio.create_task(fetch(None))

    preferred_releases: list[ParsedRelease] = []

    if preferred_task is not None:
        try:
            preferred_releases = await preferred_task
        except ProwlarrError as exc:
            # Degrade rather than fail: the unfiltered search is still running
            # and its results include this indexer's anyway.
            logger.warning("preferred-indexer search failed, falling back: %s", exc)
        else:
            yield SearchPhase(
                phase=PHASE_PREFERRED,
                releases=preferred_releases,
                recommendations=_recommendations(preferred_releases, scorable),
            )

    error: str | None = None
    all_releases: list[ParsedRelease] = []
    try:
        all_releases = await all_task
    except ProwlarrError as exc:
        logger.warning("all-indexer search failed: %s", exc)
        error = str(exc)

    already_sent = {release.guid for release in preferred_releases if release.guid}
    fresh = [
        release
        for release in all_releases
        if not release.guid or release.guid not in already_sent
    ]
    merged = [*preferred_releases, *fresh]
    effective = preferred_indexer_candidates(merged, preferred_indexer_id)

    yield SearchPhase(
        phase=PHASE_ALL,
        releases=fresh,
        recommendations=_recommendations(effective, scorable),
        error=error,
    )


async def stream_search_ndjson(**kwargs: Any) -> AsyncIterator[str]:
    """:func:`stream_search` rendered as NDJSON lines, ready for the wire."""
    async for phase in stream_search(**kwargs):
        yield phase.to_ndjson_line()
