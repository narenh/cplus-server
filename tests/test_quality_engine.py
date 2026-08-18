"""Quality profile rule engine tests.

Candidates are hand-built rather than parsed, so a rule-engine failure can never
be a parser failure in disguise.  The one exception is
:func:`test_engine_consumes_real_parser_output`, which wires the two together.
"""

from __future__ import annotations

from cplus_service.quality.engine import (
    apply_filters,
    preferred_indexer_candidates,
    rank,
    recommend,
)
from cplus_service.quality.models import (
    AudioMatchRule,
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
