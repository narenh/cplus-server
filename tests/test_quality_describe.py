"""Reading a profile back in plain English.

What the admin UI prints at the top of the builder, and in the profile list. It
is generated from the same objects the engine consumes, so these tests are
about whether it reads like something a person wrote — a wrong reading is worse
than none, because it is believed.
"""

from __future__ import annotations

from cplus_service.quality.describe import (
    describe_choice,
    describe_filter,
    describe_match,
    describe_preference,
    describe_tie_break,
    ordinal,
    summarise,
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
    TieBreak,
    default_profile,
)
from cplus_service.release.models import AudioTag, Resolution, Source


def test_a_size_reads_the_way_an_admin_would_write_it() -> None:
    # 15, not 15.0 — and 15.5 when that is what was typed.
    assert describe_filter(SizeCapGbRule(value=15)) == "anything over 15 GB"
    assert describe_filter(SizeCapGbRule(value=15.5)) == "anything over 15.5 GB"


def test_filters_read_as_the_object_of_never_grab() -> None:
    assert "CAM" in describe_filter(ExcludePrereleaseRule())
    assert describe_filter(KeywordExcludeRule(values=["yify", "korsub"])) == (
        "titles containing 'yify' or 'korsub'"
    )


def test_a_disabled_filter_says_it_is_doing_nothing() -> None:
    # A rule left in the profile switched off is not the same as no rule, and
    # the reading has to be honest about which one an admin is looking at.
    assert "nothing" in describe_filter(ExcludePrereleaseRule(enabled=False))
    assert "nothing" in describe_filter(KeywordExcludeRule(values=[]))


def test_ordered_preferences_read_as_an_order() -> None:
    assert describe_preference(
        ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P])
    ) == "resolution: 2160p → 1080p"
    assert describe_preference(HdrMatchRule(values=["DV", "SDR"])) == (
        "dynamic range: DV → SDR"
    )
    assert describe_preference(AudioMatchRule(values=[AudioTag.ATMOS])) == "audio: Atmos"


def test_an_empty_ordered_rule_says_it_decides_nothing() -> None:
    assert "decides nothing" in describe_preference(ResolutionOrderRule(values=[]))


def test_the_size_preference_distinguishes_its_soft_cap_from_a_filter() -> None:
    assert describe_preference(SizeRule(direction=SizeDirection.LARGEST)) == "the biggest file"
    assert "demoted" in describe_preference(SizeRule(cap_gb=20))
    assert describe_preference(SizeRule(direction=SizeDirection.SMALLEST)) == (
        "the smallest file"
    )


def test_a_match_reads_as_a_kind_of_release() -> None:
    assert describe_match(
        ChoiceMatch(
            resolutions=[Resolution.UHD_2160P], sources=[Source.WEB_DL], max_size_gb=15
        )
    ) == "4K · WEB-DL · under 15 GB"


def test_an_unconstrained_match_says_anything() -> None:
    assert describe_match(ChoiceMatch()) == "anything"


def test_alternatives_within_a_field_read_as_alternatives() -> None:
    assert describe_match(ChoiceMatch(hdr=["DV", "HDR10+", "HDR10"])) == (
        "DV, HDR10+ or HDR10"
    )


def test_both_size_bounds_read_as_a_range() -> None:
    assert describe_match(ChoiceMatch(min_size_gb=8, max_size_gb=15)) == (
        "between 8 GB and 15 GB"
    )
    assert describe_match(ChoiceMatch(min_size_gb=8)) == "over 8 GB"


def test_a_choice_reads_as_its_kind_plus_how_it_picks() -> None:
    choice = Choice(
        match=ChoiceMatch(resolutions=[Resolution.UHD_2160P]), tie_break=TieBreak.BIGGEST
    )
    assert describe_choice(choice) == "4K, biggest file"


def test_a_choice_with_no_tie_break_says_nothing_about_ties() -> None:
    # Empty is the default and means "the tie-breakers decide", which the
    # section below the choice already says. Repeating it in every row would
    # bury the part that differs.
    assert describe_tie_break(Choice()) == ""
    assert describe_choice(Choice()) == "anything"


def test_closest_to_a_size_without_a_size_says_what_will_actually_happen() -> None:
    assert describe_tie_break(Choice(tie_break=TieBreak.CLOSEST_TO_GB)) == "biggest file"
    assert describe_tie_break(
        Choice(tie_break=TieBreak.CLOSEST_TO_GB, tie_break_gb=12)
    ) == "closest to 12 GB"


def test_ordinals_are_the_ones_people_write() -> None:
    assert [ordinal(index) for index in range(4)] == ["1st", "2nd", "3rd", "4th"]
    assert ordinal(10) == "11th"
    assert ordinal(20) == "21st"


def test_summarising_a_whole_profile_splits_it_into_its_three_questions() -> None:
    profile = QualityProfile(
        name="4K or big HD",
        rules=[
            ExcludePrereleaseRule(),
            RepackProperPriorityRule(),
            SizeRule(direction=SizeDirection.LARGEST),
        ],
        choices=[
            Choice(
                match=ChoiceMatch(
                    resolutions=[Resolution.UHD_2160P], sources=[Source.WEB_DL]
                ),
                tie_break=TieBreak.BIGGEST,
            ),
            Choice(
                match=ChoiceMatch(resolutions=[Resolution.FHD_1080P], max_size_gb=15),
                tie_break=TieBreak.BIGGEST,
            ),
        ],
    )

    summary = summarise(profile)

    assert len(summary.never_grab) == 1
    assert summary.choices == ["4K · WEB-DL, biggest file", "1080p · under 15 GB, biggest file"]
    assert len(summary.tie_breaks) == 2
    assert summary.is_empty is False


def test_an_empty_profile_says_so() -> None:
    assert summarise(QualityProfile(name="All")).is_empty is True
    assert summarise(default_profile()).is_empty is False


def test_a_catch_all_choice_that_is_not_last_is_called_out() -> None:
    # Every choice after it is dead, and nothing about the form says so.
    profile = QualityProfile(
        name="p",
        choices=[
            Choice(tie_break=TieBreak.BIGGEST),
            Choice(match=ChoiceMatch(resolutions=[Resolution.UHD_2160P])),
        ],
    )
    assert summarise(profile).catch_all_choice == 0


def test_a_catch_all_choice_at_the_bottom_is_fine() -> None:
    profile = QualityProfile(
        name="p",
        choices=[
            Choice(match=ChoiceMatch(resolutions=[Resolution.UHD_2160P])),
            Choice(tie_break=TieBreak.BIGGEST),
        ],
    )
    assert summarise(profile).catch_all_choice is None


# --------------------------------------------------------------------------- #
# The preview's sample cast
#
# It only teaches an admin something if the releases in it actually differ in
# the ways the rules can see.
# --------------------------------------------------------------------------- #


def test_the_sample_cast_covers_what_the_rules_can_ask_about() -> None:
    from cplus_service.quality.samples import sample_releases

    samples = sample_releases()

    resolutions = {release.resolution for release in samples}
    sources = {release.source for release in samples}
    assert {Resolution.UHD_2160P, Resolution.FHD_1080P, Resolution.UNKNOWN} <= resolutions
    assert {Source.WEB_DL, Source.REMUX, Source.BLURAY} <= sources

    # A pre-release to drop, a REPACK to promote, and a spread of sizes wide
    # enough that a cap changes the answer.
    assert any(release.is_prerelease for release in samples)
    assert any(release.is_repack_or_proper for release in samples)
    assert min(r.size_gb for r in samples) < 2 < 40 < max(r.size_gb for r in samples)
    assert any(release.audio_tags for release in samples)
    assert any("DV" in release.hdr_tags for release in samples)


def test_the_sample_cast_is_the_same_every_time_it_is_drawn() -> None:
    # An admin comparing two edits should see their rules change, not the ages
    # of the sample releases.
    from cplus_service.quality.samples import sample_releases

    assert [r.publish_date for r in sample_releases()] == [
        r.publish_date for r in sample_releases()
    ]
