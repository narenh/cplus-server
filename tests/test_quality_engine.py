"""Quality profile rule engine tests.

Candidates are hand-built rather than parsed, so a rule-engine failure can never
be a parser failure in disguise.  The one exception is
:func:`test_engine_consumes_real_parser_output`, which wires the two together.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cplus_service.quality.engine import (
    apply_filters,
    choice_index,
    explain,
    matches,
    preferred_indexer_candidates,
    rank,
    recommend,
    rejection,
)
from cplus_service.quality.models import (
    AudioMatchRule,
    Choice,
    ChoiceMatch,
    ExcludePrereleaseRule,
    HdrMatchRule,
    KeywordExcludeRule,
    QualityProfile,
    RepackProperPriorityRule,
    ResolutionOrderRule,
    SizeCapGbRule,
    SizeDirection,
    SizeRule,
    SourceOrderRule,
    TieBreak,
    default_profile,
)
from cplus_service.release.models import (
    BYTES_PER_GB,
    AudioTag,
    ParsedRelease,
    Resolution,
    Source,
)
from cplus_service.release.parser import parse_prowlarr_results

GB = BYTES_PER_GB


def make(
    title: str = "Movie.2024.1080p.WEB-DL-GRP",
    *,
    resolution: Resolution = Resolution.FHD_1080P,
    source: Source = Source.WEB_DL,
    dv_profile: int = 0,
    is_hdr10plus: bool = False,
    is_hdr: bool = False,
    has_atmos: bool = False,
    has_dtsx: bool = False,
    has_truehd: bool = False,
    is_repack_or_proper: bool = False,
    is_prerelease: bool = False,
    is_full_disc: bool = False,
    base_title: str = "movie 2024",
    size_gb: float | None = 10.0,
    indexer_id: int | None = 1,
    guid: str = "",
    seeders: int | None = None,
    publish_date: datetime | None = None,
) -> ParsedRelease:
    return ParsedRelease(
        title=title,
        guid=guid or title,
        resolution=resolution,
        source=source,
        dv_profile=dv_profile,
        is_hdr10plus=is_hdr10plus,
        is_hdr=is_hdr,
        has_atmos=has_atmos,
        has_dtsx=has_dtsx,
        has_truehd=has_truehd,
        is_repack_or_proper=is_repack_or_proper,
        is_prerelease=is_prerelease,
        is_full_disc=is_full_disc,
        base_title=base_title,
        size_bytes=None if size_gb is None else int(size_gb * GB),
        indexer_id=indexer_id,
        seeders=seeders,
        publish_date=publish_date,
    )


# --------------------------------------------------------------------------- #
# Empty / degenerate cases
# --------------------------------------------------------------------------- #


def test_no_candidates_yields_no_recommendation() -> None:
    assert recommend([], default_profile()) is None


def test_profile_with_no_rules_returns_the_first_candidate() -> None:
    first, second = make(guid="a"), make(guid="b")
    assert recommend([first, second], QualityProfile(name="empty")) is first


def test_everything_filtered_out_is_none_not_an_error() -> None:
    profile = QualityProfile(name="strict", rules=[ExcludePrereleaseRule()])
    candidates = [make(is_prerelease=True), make(is_prerelease=True)]
    assert recommend(candidates, profile) is None


def test_full_discs_are_never_eligible_even_if_handed_in() -> None:
    disc = make(guid="disc", is_full_disc=True)
    web = make(guid="web")
    assert recommend([disc, web], QualityProfile(name="p")) is web


# --------------------------------------------------------------------------- #
# Filter rules
# --------------------------------------------------------------------------- #


def test_exclude_prerelease_drops_prereleases() -> None:
    profile = QualityProfile(name="p", rules=[ExcludePrereleaseRule()])
    cam, web = make(guid="cam", is_prerelease=True), make(guid="web")
    assert apply_filters([cam, web], profile) == [web]


def test_exclude_prerelease_is_off_when_absent_from_the_profile() -> None:
    cam = make(guid="cam", is_prerelease=True)
    assert recommend([cam], QualityProfile(name="p")) is cam


def test_exclude_prerelease_can_be_explicitly_disabled() -> None:
    profile = QualityProfile(name="p", rules=[ExcludePrereleaseRule(enabled=False)])
    cam = make(guid="cam", is_prerelease=True)
    assert recommend([cam], profile) is cam


def test_keyword_exclude_is_case_insensitive_on_the_raw_title() -> None:
    profile = QualityProfile(name="p", rules=[KeywordExcludeRule(values=["yify", "HDTS"])])
    yify = make("Movie.2024.1080p.BluRay.x264-YIFY", guid="yify")
    hdts = make("Movie.2024.hdts.1080p-GRP", guid="hdts")
    good = make(guid="good")

    assert apply_filters([yify, hdts, good], profile) == [good]


def test_keyword_exclude_ignores_blank_entries() -> None:
    profile = QualityProfile(name="p", rules=[KeywordExcludeRule(values=["", "   "])])
    candidate = make()
    assert apply_filters([candidate], profile) == [candidate]


def test_size_cap_filter_drops_oversized_releases() -> None:
    profile = QualityProfile(name="p", rules=[SizeCapGbRule(value=20.0)])
    small, big = make(guid="small", size_gb=15), make(guid="big", size_gb=60)
    assert apply_filters([small, big], profile) == [small]


def test_size_cap_filter_keeps_releases_with_unknown_size() -> None:
    profile = QualityProfile(name="p", rules=[SizeCapGbRule(value=20.0)])
    unknown = make(guid="unknown", size_gb=None)
    assert apply_filters([unknown], profile) == [unknown]


def test_size_cap_filter_boundary_is_inclusive() -> None:
    profile = QualityProfile(name="p", rules=[SizeCapGbRule(value=20.0)])
    exact = make(guid="exact", size_gb=20.0)
    assert apply_filters([exact], profile) == [exact]


# --------------------------------------------------------------------------- #
# Preference rules
# --------------------------------------------------------------------------- #


def test_resolution_order() -> None:
    profile = QualityProfile(
        name="p",
        rules=[ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P])],
    )
    uhd = make(guid="uhd", resolution=Resolution.UHD_2160P)
    fhd = make(guid="fhd", resolution=Resolution.FHD_1080P)

    assert recommend([fhd, uhd], profile) is uhd


def test_unlisted_resolution_ranks_last_but_is_not_filtered_out() -> None:
    profile = QualityProfile(
        name="p", rules=[ResolutionOrderRule(values=[Resolution.UHD_2160P])]
    )
    sd = make(guid="sd", resolution=Resolution.SD_480P)
    uhd = make(guid="uhd", resolution=Resolution.UHD_2160P)

    assert recommend([sd, uhd], profile) is uhd
    assert recommend([sd], profile) is sd


def test_source_order() -> None:
    profile = QualityProfile(
        name="p",
        rules=[SourceOrderRule(values=[Source.REMUX, Source.BLURAY, Source.WEB_DL])],
    )
    web = make(guid="web", source=Source.WEB_DL)
    remux = make(guid="remux", source=Source.REMUX)
    bluray = make(guid="bluray", source=Source.BLURAY)

    assert recommend([web, bluray, remux], profile) is remux


def test_hdr_match_prefers_the_best_listed_tag() -> None:
    profile = QualityProfile(
        name="p", rules=[HdrMatchRule(values=["DV_P8", "HDR10+", "HDR10", "SDR"])]
    )
    sdr = make(guid="sdr")
    hdr10 = make(guid="hdr10", is_hdr=True)
    hdr10plus = make(guid="hdr10plus", is_hdr10plus=True)
    dv8 = make(guid="dv8", dv_profile=8, is_hdr=True)

    assert recommend([sdr, hdr10, hdr10plus, dv8], profile) is dv8
    assert recommend([sdr, hdr10, hdr10plus], profile) is hdr10plus
    assert recommend([sdr, hdr10], profile) is hdr10


def test_hdr_match_coarse_dv_token_matches_any_profile() -> None:
    profile = QualityProfile(name="p", rules=[HdrMatchRule(values=["DV", "HDR10"])])
    dv5 = make(guid="dv5", dv_profile=5)
    hdr10 = make(guid="hdr10", is_hdr=True)

    assert recommend([hdr10, dv5], profile) is dv5


def test_hdr_match_distinguishes_dv_profiles() -> None:
    profile = QualityProfile(name="p", rules=[HdrMatchRule(values=["DV_P8", "DV_P5"])])
    dv5 = make(guid="dv5", dv_profile=5)
    dv8 = make(guid="dv8", dv_profile=8)

    assert recommend([dv5, dv8], profile) is dv8


def test_audio_match_treats_the_three_formats_as_distinct() -> None:
    profile = QualityProfile(
        name="p",
        rules=[AudioMatchRule(values=[AudioTag.DTSX, AudioTag.ATMOS, AudioTag.TRUEHD])],
    )
    atmos = make(guid="atmos", has_atmos=True)
    dtsx = make(guid="dtsx", has_dtsx=True)
    truehd = make(guid="truehd", has_truehd=True)
    none = make(guid="none")

    assert recommend([none, truehd, atmos, dtsx], profile) is dtsx
    assert recommend([none, truehd, atmos], profile) is atmos
    assert recommend([none, truehd], profile) is truehd


def test_audio_match_scores_a_release_by_its_best_tag() -> None:
    profile = QualityProfile(
        name="p", rules=[AudioMatchRule(values=[AudioTag.ATMOS, AudioTag.TRUEHD])]
    )
    both = make(guid="both", has_atmos=True, has_truehd=True)
    truehd_only = make(guid="truehd", has_truehd=True)

    assert recommend([truehd_only, both], profile) is both


# --------------------------------------------------------------------------- #
# REPACK / PROPER priority
# --------------------------------------------------------------------------- #


def test_repack_beats_the_base_release_of_the_same_title() -> None:
    profile = QualityProfile(name="p", rules=[RepackProperPriorityRule()])
    base = make(guid="base", base_title="movie 2024")
    repack = make(guid="repack", base_title="movie 2024", is_repack_or_proper=True)

    assert recommend([base, repack], profile) is repack


def test_repack_of_a_different_title_does_not_demote_an_unrelated_base() -> None:
    # Title-diffed, not tag-matched: the REPACK of "other movie" must not push
    # "movie"'s only release down the list.
    profile = QualityProfile(
        name="p",
        rules=[
            RepackProperPriorityRule(),
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
        ],
    )
    wanted = make(guid="wanted", base_title="movie 2024", resolution=Resolution.UHD_2160P)
    other_repack = make(
        guid="other",
        base_title="other movie 2024",
        resolution=Resolution.FHD_1080P,
        is_repack_or_proper=True,
    )

    assert recommend([other_repack, wanted], profile) is wanted


def test_repack_priority_can_be_disabled() -> None:
    profile = QualityProfile(name="p", rules=[RepackProperPriorityRule(enabled=False)])
    base = make(guid="base")
    repack = make(guid="repack", is_repack_or_proper=True)

    assert recommend([base, repack], profile) is base


def test_repack_priority_yields_to_later_rules_only_within_the_same_title() -> None:
    profile = QualityProfile(
        name="p",
        rules=[
            RepackProperPriorityRule(),
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
        ],
    )
    base_uhd = make(guid="base_uhd", resolution=Resolution.UHD_2160P)
    repack_fhd = make(
        guid="repack_fhd", resolution=Resolution.FHD_1080P, is_repack_or_proper=True
    )

    # Repack priority is listed first, so it outranks resolution.
    assert recommend([base_uhd, repack_fhd], profile) is repack_fhd


# --------------------------------------------------------------------------- #
# Size preference (distinct from the size filter)
# --------------------------------------------------------------------------- #


def test_size_preference_largest() -> None:
    profile = QualityProfile(name="p", rules=[SizeRule(direction=SizeDirection.LARGEST)])
    small, big = make(guid="small", size_gb=5), make(guid="big", size_gb=50)
    assert recommend([small, big], profile) is big


def test_size_preference_smallest() -> None:
    profile = QualityProfile(name="p", rules=[SizeRule(direction=SizeDirection.SMALLEST)])
    small, big = make(guid="small", size_gb=5), make(guid="big", size_gb=50)
    assert recommend([small, big], profile) is small


def test_size_preference_cap_demotes_but_does_not_eliminate() -> None:
    profile = QualityProfile(
        name="p", rules=[SizeRule(direction=SizeDirection.LARGEST, cap_gb=20.0)]
    )
    under, over = make(guid="under", size_gb=15), make(guid="over", size_gb=60)

    # Under the cap wins even though "largest" would otherwise pick the 60 GB one.
    assert recommend([over, under], profile) is under
    # But an over-cap release is still recommendable when it is all there is.
    assert recommend([over], profile) is over


def test_size_preference_cap_prefers_the_over_cap_release_closest_to_the_cap() -> None:
    profile = QualityProfile(
        name="p", rules=[SizeRule(direction=SizeDirection.LARGEST, cap_gb=20.0)]
    )
    just_over, way_over = make(guid="just", size_gb=22), make(guid="way", size_gb=90)
    assert recommend([way_over, just_over], profile) is just_over


def test_size_filter_and_size_preference_are_separate_concepts() -> None:
    # The filter eliminates; the preference only reorders.  With both present the
    # filter has already removed the 60 GB release before ranking runs.
    profile = QualityProfile(
        name="p",
        rules=[SizeCapGbRule(value=20.0), SizeRule(direction=SizeDirection.LARGEST)],
    )
    small, mid, big = (
        make(guid="small", size_gb=5),
        make(guid="mid", size_gb=18),
        make(guid="big", size_gb=60),
    )

    assert recommend([small, mid, big], profile) is mid


def test_unknown_size_ranks_last_under_a_size_preference() -> None:
    profile = QualityProfile(name="p", rules=[SizeRule(direction=SizeDirection.SMALLEST)])
    unknown = make(guid="unknown", size_gb=None)
    known = make(guid="known", size_gb=50)

    assert recommend([unknown, known], profile) is known


# --------------------------------------------------------------------------- #
# Rule ordering
# --------------------------------------------------------------------------- #


def test_preference_rules_apply_in_profile_order() -> None:
    uhd_web = make(guid="uhd_web", resolution=Resolution.UHD_2160P, source=Source.WEB_DL)
    fhd_remux = make(guid="fhd_remux", resolution=Resolution.FHD_1080P, source=Source.REMUX)

    resolution_first = QualityProfile(
        name="res-first",
        rules=[
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
            SourceOrderRule(values=[Source.REMUX, Source.WEB_DL]),
        ],
    )
    source_first = QualityProfile(
        name="src-first",
        rules=[
            SourceOrderRule(values=[Source.REMUX, Source.WEB_DL]),
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
        ],
    )

    assert recommend([uhd_web, fhd_remux], resolution_first) is uhd_web
    assert recommend([uhd_web, fhd_remux], source_first) is fhd_remux


def test_later_rules_break_ties_left_by_earlier_ones() -> None:
    profile = QualityProfile(
        name="p",
        rules=[
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
            AudioMatchRule(values=[AudioTag.ATMOS]),
            SizeRule(direction=SizeDirection.LARGEST),
        ],
    )
    uhd_plain_big = make(guid="plain", resolution=Resolution.UHD_2160P, size_gb=80)
    uhd_atmos_small = make(
        guid="atmos", resolution=Resolution.UHD_2160P, has_atmos=True, size_gb=20
    )
    uhd_atmos_big = make(
        guid="atmos_big", resolution=Resolution.UHD_2160P, has_atmos=True, size_gb=40
    )

    ordered = rank([uhd_plain_big, uhd_atmos_small, uhd_atmos_big], profile)
    assert [r.guid for r in ordered] == ["atmos_big", "atmos", "plain"]


def test_ties_that_survive_every_rule_keep_prowlarr_order() -> None:
    profile = default_profile()
    first = make(guid="first", resolution=Resolution.UHD_2160P, size_gb=30)
    second = make(guid="second", resolution=Resolution.UHD_2160P, size_gb=30)

    assert recommend([first, second], profile) is first
    assert [r.guid for r in rank([first, second], profile)] == ["first", "second"]


# --------------------------------------------------------------------------- #
# Global preferred-indexer hard filter (not a profile rule)
# --------------------------------------------------------------------------- #


def test_preferred_indexer_none_means_all_indexers() -> None:
    candidates = [make(guid="a", indexer_id=1), make(guid="b", indexer_id=2)]
    assert preferred_indexer_candidates(candidates, None) == candidates


def test_preferred_indexer_restricts_when_that_indexer_returned_something() -> None:
    a, b = make(guid="a", indexer_id=1), make(guid="b", indexer_id=2)
    assert preferred_indexer_candidates([a, b], 2) == [b]


def test_preferred_indexer_falls_back_to_all_when_its_subset_is_empty() -> None:
    # An empty preferred subset must not turn into "no recommendation".
    a, b = make(guid="a", indexer_id=1), make(guid="b", indexer_id=2)
    assert preferred_indexer_candidates([a, b], 99) == [a, b]
    assert recommend(preferred_indexer_candidates([a, b], 99), default_profile()) is not None


# --------------------------------------------------------------------------- #
# End-to-end with the real parser
# --------------------------------------------------------------------------- #


def test_engine_consumes_real_parser_output() -> None:
    raws = [
        {"title": "Movie.2024.COMPLETE.UHD.BLURAY-TERMiNAL", "guid": "disc", "size": 80 * GB},
        {"title": "Movie.2024.1080p.WEB-DL.DDP5.1-GRP", "guid": "fhd", "size": 8 * GB},
        {
            "title": "Movie.2024.2160p.WEB-DL.DV.P8.HDR10+.DDP5.1.Atmos.HEVC-FLUX",
            "guid": "uhd",
            "size": 25 * GB,
        },
        {"title": "Movie.2024.HDCAM.1080p.x264-GRP", "guid": "cam", "size": 2 * GB},
    ]
    candidates = parse_prowlarr_results(raws)
    assert [c.guid for c in candidates] == ["fhd", "uhd", "cam"]  # full disc already gone

    profile = QualityProfile(
        name="4k",
        rules=[
            ExcludePrereleaseRule(),
            SizeCapGbRule(value=50.0),
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
            HdrMatchRule(values=["DV_P8", "HDR10+", "HDR10", "SDR"]),
            SizeRule(direction=SizeDirection.LARGEST),
        ],
    )

    best = recommend(candidates, profile)
    assert best is not None
    assert best.guid == "uhd"
    assert best.dv_profile == 8
    assert best.is_hdr10plus is True
    assert best.has_atmos is True


# --------------------------------------------------------------------------- #
# Choices
#
# The thing an ordered preference list cannot say: "the best 4K WEB copy, or
# failing that the biggest 1080p under 15 GB". Two wants, different size rules.
# --------------------------------------------------------------------------- #


UHD_WEB = Choice(
    match=ChoiceMatch(resolutions=[Resolution.UHD_2160P], sources=[Source.WEB_DL]),
    tie_break=TieBreak.BIGGEST,
)
HD_UNDER_15 = Choice(
    match=ChoiceMatch(resolutions=[Resolution.FHD_1080P], max_size_gb=15),
    tie_break=TieBreak.BIGGEST,
)


def test_a_first_choice_beats_a_second_choice_however_good_the_second_is() -> None:
    profile = QualityProfile(
        name="4K or big HD",
        choices=[UHD_WEB, HD_UNDER_15],
        # A tie-breaker that would pick the huge 1080p if choices did not exist.
        rules=[SizeRule(direction=SizeDirection.LARGEST)],
    )
    small_uhd = make(guid="uhd", resolution=Resolution.UHD_2160P, size_gb=9)
    big_hd = make(guid="hd", resolution=Resolution.FHD_1080P, size_gb=14.5)

    assert recommend([big_hd, small_uhd], profile) is small_uhd


def test_each_choice_carries_its_own_size_rule() -> None:
    # The whole point: 4K is unbounded here, 1080p is capped at 15 GB, and no
    # single ordered list of rules can hold both of those at once.
    profile = QualityProfile(name="p", choices=[UHD_WEB, HD_UNDER_15])
    huge_uhd = make(guid="uhd", resolution=Resolution.UHD_2160P, size_gb=60)
    over_cap_hd = make(guid="hd-big", resolution=Resolution.FHD_1080P, size_gb=40)
    under_cap_hd = make(guid="hd-ok", resolution=Resolution.FHD_1080P, size_gb=12)

    ordered = rank([over_cap_hd, under_cap_hd, huge_uhd], profile)
    assert [r.guid for r in ordered] == ["uhd", "hd-ok", "hd-big"]


def test_a_release_matching_no_choice_ranks_last_but_is_still_eligible() -> None:
    profile = QualityProfile(name="p", choices=[UHD_WEB])
    unmatched = make(guid="sd", resolution=Resolution.SD_480P, size_gb=1)

    # Last, not dropped — and still the pick when it is all there is.
    assert choice_index(unmatched, profile) == 1
    assert recommend([unmatched], profile) is unmatched


def test_a_profile_with_no_choices_ranks_exactly_as_it_did_before() -> None:
    # Every profile stored before choices existed has none, so this is the
    # compatibility guarantee, not a nicety.
    rules = [ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P])]
    hd = make(guid="hd", resolution=Resolution.FHD_1080P)
    uhd = make(guid="uhd", resolution=Resolution.UHD_2160P)

    assert [r.guid for r in rank([hd, uhd], QualityProfile(name="p", rules=rules))] == [
        "uhd",
        "hd",
    ]


def test_a_choice_tie_break_decides_before_the_preference_rules() -> None:
    profile = QualityProfile(
        name="p",
        choices=[Choice(tie_break=TieBreak.SMALLEST)],
        rules=[SizeRule(direction=SizeDirection.LARGEST)],
    )
    small = make(guid="small", size_gb=2)
    big = make(guid="big", size_gb=40)

    assert recommend([big, small], profile) is small


def test_preference_rules_break_the_ties_a_choice_leaves() -> None:
    profile = QualityProfile(
        name="p",
        choices=[Choice(match=ChoiceMatch(resolutions=[Resolution.UHD_2160P]))],
        rules=[SourceOrderRule(values=[Source.REMUX, Source.WEB_DL])],
    )
    web = make(guid="web", resolution=Resolution.UHD_2160P, source=Source.WEB_DL)
    remux = make(guid="remux", resolution=Resolution.UHD_2160P, source=Source.REMUX)

    assert recommend([web, remux], profile) is remux


def test_a_filter_still_beats_every_choice() -> None:
    profile = QualityProfile(
        name="p",
        choices=[Choice(match=ChoiceMatch(resolutions=[Resolution.UHD_2160P]))],
        rules=[SizeCapGbRule(value=20)],
    )
    over_cap = make(guid="huge", resolution=Resolution.UHD_2160P, size_gb=60)
    small = make(guid="small", resolution=Resolution.SD_480P, size_gb=1)

    # First choice, and still gone: filters run before choices are consulted.
    assert recommend([over_cap, small], profile) is small


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def test_an_empty_match_matches_anything() -> None:
    assert matches(make(), ChoiceMatch()) is True
    assert ChoiceMatch().is_anything is True


def test_a_match_field_is_satisfied_by_any_of_its_values() -> None:
    match = ChoiceMatch(sources=[Source.WEB_DL, Source.WEBRIP])
    assert matches(make(source=Source.WEBRIP), match) is True
    assert matches(make(source=Source.BLURAY), match) is False


def test_tag_fields_match_when_the_release_carries_any_listed_tag() -> None:
    dv_or_hdr = ChoiceMatch(hdr=["DV", "HDR10+"])
    assert matches(make(dv_profile=8), dv_or_hdr) is True
    assert matches(make(is_hdr10plus=True), dv_or_hdr) is True
    assert matches(make(is_hdr=True), dv_or_hdr) is False

    atmos = ChoiceMatch(audio=[AudioTag.ATMOS])
    assert matches(make(has_atmos=True, has_truehd=True), atmos) is True
    assert matches(make(has_truehd=True), atmos) is False


def test_every_listed_field_has_to_match() -> None:
    match = ChoiceMatch(resolutions=[Resolution.UHD_2160P], sources=[Source.WEB_DL])
    assert matches(make(resolution=Resolution.UHD_2160P, source=Source.WEB_DL), match)
    assert not matches(make(resolution=Resolution.UHD_2160P, source=Source.REMUX), match)


def test_an_unknown_size_never_satisfies_a_size_bound() -> None:
    # Not a guess in either direction: the release falls through to a later
    # choice rather than being admitted on evidence nobody has.
    under_15 = ChoiceMatch(max_size_gb=15)
    assert matches(make(size_gb=None), under_15) is False
    assert matches(make(size_gb=None), ChoiceMatch(min_size_gb=1)) is False
    # ...while the size *filter* keeps it, because there the cost of guessing
    # wrong is elimination rather than a lower ranking.
    kept = apply_filters([make(size_gb=None)], QualityProfile(rules=[SizeCapGbRule(value=15)]))
    assert len(kept) == 1


def test_size_bounds_are_inclusive_at_both_ends() -> None:
    between = ChoiceMatch(min_size_gb=10, max_size_gb=15)
    assert matches(make(size_gb=10), between) is True
    assert matches(make(size_gb=15), between) is True
    assert matches(make(size_gb=9.9), between) is False
    assert matches(make(size_gb=15.1), between) is False


# --------------------------------------------------------------------------- #
# Tie-breaks
# --------------------------------------------------------------------------- #


def test_closest_to_a_size_wins_from_either_side() -> None:
    profile = QualityProfile(
        name="p", choices=[Choice(tie_break=TieBreak.CLOSEST_TO_GB, tie_break_gb=10)]
    )
    under = make(guid="under", size_gb=8)
    over = make(guid="over", size_gb=10.5)
    far = make(guid="far", size_gb=40)

    assert [r.guid for r in rank([far, under, over], profile)] == ["over", "under", "far"]


def test_newest_and_most_seeders_order_on_their_own_fields() -> None:
    old = make(guid="old", publish_date=datetime(2024, 1, 1, tzinfo=UTC), seeders=900)
    new = make(guid="new", publish_date=datetime(2026, 1, 1, tzinfo=UTC), seeders=3)

    newest = QualityProfile(name="p", choices=[Choice(tie_break=TieBreak.NEWEST)])
    seeders = QualityProfile(name="p", choices=[Choice(tie_break=TieBreak.MOST_SEEDERS)])

    assert recommend([old, new], newest) is new
    assert recommend([new, old], seeders) is old


def test_a_release_a_tie_break_cannot_speak_about_ranks_behind_ones_it_can() -> None:
    # Not treated as zero seeders or as an ancient release: unknown is its own
    # thing, and it loses to anything the tie-break can actually compare.
    known = make(guid="known", seeders=1)
    unknown = make(guid="unknown", seeders=None)
    profile = QualityProfile(name="p", choices=[Choice(tie_break=TieBreak.MOST_SEEDERS)])

    assert [r.guid for r in rank([unknown, known], profile)] == ["known", "unknown"]


# --------------------------------------------------------------------------- #
# explain()
# --------------------------------------------------------------------------- #


def test_explain_reports_every_candidate_kept_or_dropped() -> None:
    profile = QualityProfile(
        name="p", rules=[ExcludePrereleaseRule()], choices=[UHD_WEB]
    )
    uhd = make(guid="uhd", resolution=Resolution.UHD_2160P, size_gb=20)
    hd = make(guid="hd", size_gb=8)
    cam = make(guid="cam", is_prerelease=True)

    judged = explain([cam, hd, uhd], profile)

    assert [j.release.guid for j in judged] == ["uhd", "hd", "cam"]
    assert judged[0].kept and judged[0].position == 0 and judged[0].choice == 0
    # Kept, ranked last, and matching no choice — three different facts.
    assert judged[1].kept and judged[1].position == 1 and judged[1].choice is None
    assert not judged[2].kept


def test_explain_names_the_filter_that_dropped_each_release() -> None:
    profile = QualityProfile(
        name="p",
        rules=[
            ExcludePrereleaseRule(),
            KeywordExcludeRule(values=["korsub"]),
            SizeCapGbRule(value=20),
        ],
    )
    dropped = {
        j.release.guid: j.dropped_by
        for j in explain(
            [
                make(guid="cam", is_prerelease=True),
                make(guid="korsub", title="Movie.2024.KORSUB.1080p"),
                make(guid="huge", size_gb=60),
            ],
            profile,
        )
    }

    assert dropped["cam"] == "pre-release"
    assert dropped["korsub"] == "title contains 'korsub'"
    # No trailing ".0" — the number is read by an admin, not a machine.
    assert dropped["huge"] == "larger than 20 GB"


def test_rejection_and_apply_filters_cannot_disagree() -> None:
    # The preview's explanation is the filter behaviour, not a second opinion
    # on it: same function, applied to one release or to a list.
    profile = QualityProfile(name="p", rules=[SizeCapGbRule(value=20)])
    candidates = [make(guid="a", size_gb=5), make(guid="b", size_gb=50)]

    survivors = {r.guid for r in apply_filters(candidates, profile)}
    assert survivors == {c.guid for c in candidates if rejection(c, profile) is None}
