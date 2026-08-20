"""Two-phase streamed search tests.

Prowlarr is replaced with a stub whose two calls can be resolved independently,
so phase ordering and the fallback rules are tested directly rather than
inferred from timing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cplus_service.prowlarr.client import ProwlarrError
from cplus_service.quality.models import (
    QualityProfile,
    ResolutionOrderRule,
    SizeCapGbRule,
)
from cplus_service.release.models import ParsedRelease, Resolution
from cplus_service.search.stream import ScorableAction, stream_search

GB = 1024**3


def release(
    guid: str, *, indexer_id: int = 1, resolution: str = "2160p", size_gb: float = 20
) -> ParsedRelease:
    return ParsedRelease(
        title=f"Movie.2024.{resolution}.WEB-DL-{guid.upper()}",
        guid=guid,
        indexer_id=indexer_id,
        resolution=Resolution(resolution),
        size_bytes=int(size_gb * GB),
        base_title="movie 2024",
    )


def action(action_id: int, *, rules: list | None = None) -> ScorableAction:
    return ScorableAction(
        id=action_id,
        name=f"Action {action_id}",
        profile=QualityProfile(
            name=f"p{action_id}",
            rules=rules
            if rules is not None
            else [ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P])],
        ),
    )


class StubProwlarr:
    """Stands in for ProwlarrClient, recording how it was called."""

    def __init__(
        self,
        *,
        preferred: list[ParsedRelease] | Exception | None = None,
        everything: list[ParsedRelease] | Exception | None = None,
    ) -> None:
        self._preferred = preferred if preferred is not None else []
        self._everything = everything if everything is not None else []
        self.calls: list[tuple[str, tuple[int, ...] | None]] = []
        self.modes: list[str] = []

    def _answer(self, term, indexer_ids):  # noqa: ANN001, ANN202
        self.calls.append((term, tuple(indexer_ids) if indexer_ids else None))
        result = self._preferred if indexer_ids else self._everything
        if isinstance(result, Exception):
            raise result
        return list(result)

    async def search_movie(self, imdb_id, *, indexer_ids=None):  # noqa: ANN001, ANN201
        self.modes.append("movie")
        return self._answer(imdb_id, indexer_ids)

    async def search_query(self, query, *, indexer_ids=None):  # noqa: ANN001, ANN201
        self.modes.append("query")
        return self._answer(query, indexer_ids)


async def collect(**kwargs):  # noqa: ANN003, ANN201
    return [phase async for phase in stream_search(**kwargs)]


# --------------------------------------------------------------------------- #
# Phase structure
# --------------------------------------------------------------------------- #


async def test_no_preferred_indexer_means_a_single_all_phase() -> None:
    prowlarr = StubProwlarr(everything=[release("a"), release("b")])

    phases = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=None,
    )

    assert [p.phase for p in phases] == ["all"]
    # Only one Prowlarr call: there is nothing distinct to fetch early.
    assert prowlarr.calls == [("tt1", None)]
    assert phases[0].recommendations == {"1": "a"}


async def test_preferred_indexer_produces_two_phases() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p1", indexer_id=7)],
        everything=[release("p1", indexer_id=7), release("a", indexer_id=2)],
    )

    phases = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=7,
    )

    assert [p.phase for p in phases] == ["preferred", "all"]
    assert set(prowlarr.calls) == {("tt1", None), ("tt1", (7,))}


async def test_both_prowlarr_calls_are_issued_concurrently() -> None:
    started = asyncio.Event()
    release_all = asyncio.Event()

    class SlowProwlarr(StubProwlarr):
        async def search_movie(self, imdb_id, *, indexer_ids=None):  # noqa: ANN001, ANN201
            if indexer_ids:
                started.set()
                return [release("fast", indexer_id=7)]
            # Only completes once the preferred call has already run, proving
            # the two were in flight together rather than serialised.
            await release_all.wait()
            return [release("slow", indexer_id=2)]

    prowlarr = SlowProwlarr()
    phases = []

    async def drain() -> None:
        async for phase in stream_search(
            prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
        ):
            phases.append(phase)

    task = asyncio.create_task(drain())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    release_all.set()
    await asyncio.wait_for(task, timeout=1)

    assert [p.phase for p in phases] == ["preferred", "all"]


# --------------------------------------------------------------------------- #
# Deduplication and merging
# --------------------------------------------------------------------------- #


async def test_all_phase_omits_releases_already_sent_in_the_preferred_phase() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("dup", indexer_id=7)],
        everything=[release("dup", indexer_id=7), release("new", indexer_id=2)],
    )

    preferred, everything = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    assert [r.guid for r in preferred.releases] == ["dup"]
    assert [r.guid for r in everything.releases] == ["new"]


async def test_the_all_phase_resends_every_action_not_just_unresolved_ones() -> None:
    # Documented contract: the client applies the last line wholesale.
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)],
        everything=[release("p", indexer_id=7), release("a", indexer_id=2)],
    )

    preferred, everything = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1), action(2)],
        preferred_indexer_id=7,
    )

    assert set(preferred.recommendations) == {"1", "2"}
    assert set(everything.recommendations) == {"1", "2"}


async def test_resent_recommendations_agree_with_the_preferred_phase() -> None:
    # The preferred subset is non-empty, so the preferred pick stays the answer
    # even though the all phase saw more candidates.
    prowlarr = StubProwlarr(
        preferred=[release("pref", indexer_id=7, resolution="1080p")],
        everything=[
            release("pref", indexer_id=7, resolution="1080p"),
            release("better", indexer_id=2, resolution="2160p"),
        ],
    )

    preferred, everything = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    assert preferred.recommendations == {"1": "pref"}
    assert everything.recommendations == {"1": "pref"}


async def test_empty_preferred_subset_falls_back_to_the_full_set() -> None:
    prowlarr = StubProwlarr(
        preferred=[],
        everything=[release("a", indexer_id=2), release("b", indexer_id=3)],
    )

    preferred, everything = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    assert preferred.releases == []
    assert preferred.recommendations == {"1": None}
    assert everything.recommendations == {"1": "a"}


async def test_an_action_whose_filters_eliminate_everything_recommends_null() -> None:
    prowlarr = StubProwlarr(everything=[release("big", size_gb=90)])
    picky = action(1, rules=[SizeCapGbRule(value=10.0)])

    (everything,) = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[picky], preferred_indexer_id=None
    )

    assert everything.recommendations == {"1": None}
    assert [r.guid for r in everything.releases] == ["big"]


# --------------------------------------------------------------------------- #
# Empty and error cases
# --------------------------------------------------------------------------- #


async def test_zero_results_still_sends_the_all_phase() -> None:
    prowlarr = StubProwlarr(everything=[])

    phases = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=None
    )

    assert [p.phase for p in phases] == ["all"]
    assert phases[0].releases == []
    assert phases[0].recommendations == {"1": None}
    assert phases[0].error is None


async def test_no_permitted_actions_still_streams_releases() -> None:
    prowlarr = StubProwlarr(everything=[release("a")])

    (everything,) = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[], preferred_indexer_id=None
    )

    assert everything.recommendations == {}
    assert [r.guid for r in everything.releases] == ["a"]


async def test_a_failing_preferred_search_degrades_to_one_phase() -> None:
    prowlarr = StubProwlarr(
        preferred=ProwlarrError("indexer down"),
        everything=[release("a", indexer_id=7), release("b", indexer_id=2)],
    )

    phases = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    assert [p.phase for p in phases] == ["all"]
    # The unfiltered results still contain the preferred indexer's releases, so
    # the hard filter is applied to them and the answer stays correct.
    assert phases[0].recommendations == {"1": "a"}
    assert phases[0].error is None


async def test_a_failing_all_search_reports_in_band_so_the_client_can_stop_loading() -> None:
    prowlarr = StubProwlarr(everything=ProwlarrError("prowlarr exploded"))

    (everything,) = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=None
    )

    assert everything.phase == "all"
    assert everything.releases == []
    assert everything.recommendations == {"1": None}
    assert everything.error is not None
    assert "exploded" in everything.error


async def test_both_searches_failing_still_terminates_the_stream() -> None:
    prowlarr = StubProwlarr(
        preferred=ProwlarrError("a"), everything=ProwlarrError("b")
    )

    phases = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    assert [p.phase for p in phases] == ["all"]
    assert phases[0].error is not None


# --------------------------------------------------------------------------- #
# Wire format and cache side effect
# --------------------------------------------------------------------------- #


async def test_ndjson_lines_are_one_object_each_and_newline_terminated() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)],
        everything=[release("p", indexer_id=7), release("a", indexer_id=2)],
    )

    lines = [
        phase.to_ndjson_line()
        for phase in await collect(
            prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
        )
    ]

    assert all(line.endswith("\n") for line in lines)
    assert all(line.count("\n") == 1 for line in lines)

    payloads = [json.loads(line) for line in lines]
    assert [p["phase"] for p in payloads] == ["preferred", "all"]
    assert set(payloads[0]) == {"phase", "releases", "recommendations"}


async def test_streamed_releases_carry_the_parser_tags() -> None:
    prowlarr = StubProwlarr(everything=[release("a")])

    (everything,) = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[], preferred_indexer_id=None
    )
    payload = json.loads(everything.to_ndjson_line())
    release_json = payload["releases"][0]

    assert release_json["guid"] == "a"
    assert release_json["resolution"] == "2160p"
    assert "hdr_tags" in release_json
    assert "audio_tags" in release_json
    assert "category" not in release_json


@pytest.mark.parametrize("preferred_id", [None, 7])
async def test_the_all_phase_is_always_last(preferred_id: int | None) -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)], everything=[release("a", indexer_id=2)]
    )

    phases = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=preferred_id,
    )

    assert phases[-1].phase == "all"


# --------------------------------------------------------------------------- #
# Free-text query mode
# --------------------------------------------------------------------------- #


async def test_a_text_query_uses_the_free_text_prowlarr_call() -> None:
    prowlarr = StubProwlarr(everything=[release("a")])

    phases = await collect(
        prowlarr=prowlarr, query="the office", actions=[action(1)], preferred_indexer_id=None
    )

    assert prowlarr.modes == ["query"]
    assert prowlarr.calls == [("the office", None)]
    assert [p.phase for p in phases] == ["all"]


async def test_a_text_query_never_recommends() -> None:
    # No quality profile can rank an arbitrary string's results, so none is
    # consulted and nothing is recommended.
    prowlarr = StubProwlarr(everything=[release("a"), release("b")])

    (everything,) = await collect(
        prowlarr=prowlarr,
        query="dune",
        actions=[action(1), action(2)],
        preferred_indexer_id=None,
    )

    assert everything.recommendations == {}
    assert [r.guid for r in everything.releases] == ["a", "b"]


async def test_a_text_query_is_a_single_phase_even_with_a_preferred_indexer() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)],
        everything=[release("a", indexer_id=2)],
    )

    phases = await collect(
        prowlarr=prowlarr, query="dune", actions=[action(1)], preferred_indexer_id=7
    )

    assert [p.phase for p in phases] == ["all"]
    # One call, unscoped: there is no recommendation to race for.
    assert prowlarr.calls == [("dune", None)]


async def test_a_failing_text_query_still_terminates_the_stream() -> None:
    prowlarr = StubProwlarr(everything=ProwlarrError("prowlarr exploded"))

    (everything,) = await collect(
        prowlarr=prowlarr, query="dune", actions=[], preferred_indexer_id=None
    )

    assert everything.phase == "all"
    assert everything.releases == []
    assert everything.error is not None


async def test_a_text_query_with_no_results_still_sends_the_all_phase() -> None:
    prowlarr = StubProwlarr(everything=[])

    phases = await collect(
        prowlarr=prowlarr, query="nothing matches this", actions=[], preferred_indexer_id=None
    )

    assert [p.phase for p in phases] == ["all"]
    assert phases[0].releases == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"imdb_id": "tt1", "query": "dune"},
    ],
)
async def test_exactly_one_of_imdb_id_or_query_is_required(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await collect(
            prowlarr=StubProwlarr(), actions=[], preferred_indexer_id=None, **kwargs
        )


# --------------------------------------------------------------------------- #
# preferred_only
# --------------------------------------------------------------------------- #


async def test_preferred_only_makes_one_scoped_call_and_one_phase() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)],
        everything=[release("a", indexer_id=2)],
    )

    phases = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=7,
        preferred_only=True,
    )

    assert prowlarr.calls == [("tt1", (7,))]
    assert [p.phase for p in phases] == ["all"]
    assert [r.guid for r in phases[0].releases] == ["p"]
    # Still scored — only the indexer scope changed, not the mode.
    assert phases[0].recommendations == {"1": "p"}


async def test_preferred_only_falls_back_to_all_when_none_is_configured() -> None:
    # The flag asks for the fast path; with nothing to be fast about, searching
    # everything is the honest answer rather than an error.
    prowlarr = StubProwlarr(everything=[release("a", indexer_id=2)])

    phases = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=None,
        preferred_only=True,
    )

    assert prowlarr.calls == [("tt1", None)]
    assert [p.phase for p in phases] == ["all"]
    assert phases[0].recommendations == {"1": "a"}


async def test_preferred_only_applies_to_text_queries_too() -> None:
    prowlarr = StubProwlarr(preferred=[release("p", indexer_id=7)])

    phases = await collect(
        prowlarr=prowlarr,
        query="dune",
        actions=[action(1)],
        preferred_indexer_id=7,
        preferred_only=True,
    )

    assert prowlarr.modes == ["query"]
    assert prowlarr.calls == [("dune", (7,))]
    assert [p.phase for p in phases] == ["all"]
    assert phases[0].recommendations == {}


async def test_the_default_is_all_indexers_not_preferred_only() -> None:
    prowlarr = StubProwlarr(
        preferred=[release("p", indexer_id=7)],
        everything=[release("p", indexer_id=7), release("a", indexer_id=2)],
    )

    phases = await collect(
        prowlarr=prowlarr, imdb_id="tt1", actions=[action(1)], preferred_indexer_id=7
    )

    # Unchanged two-phase behaviour: the unscoped call is still made.
    assert set(prowlarr.calls) == {("tt1", None), ("tt1", (7,))}
    assert [p.phase for p in phases] == ["preferred", "all"]


async def test_a_failing_preferred_only_search_still_terminates() -> None:
    prowlarr = StubProwlarr(preferred=ProwlarrError("indexer down"))

    (everything,) = await collect(
        prowlarr=prowlarr,
        imdb_id="tt1",
        actions=[action(1)],
        preferred_indexer_id=7,
        preferred_only=True,
    )

    assert everything.phase == "all"
    assert everything.error is not None
    assert everything.recommendations == {"1": None}
