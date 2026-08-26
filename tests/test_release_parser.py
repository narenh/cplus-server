"""Release parser tests.

Every field is covered with dot-delimited, space-delimited and mixed-delimiter
examples.  The dot-delimited cases are the point: the Swift implementation this
replaces matched on spaces only and silently missed the majority of real scene
titles, so a pattern that is only proven against space-delimited input is not
proven at all.
"""

from __future__ import annotations

import pytest

from cplus_service.release.models import Resolution, Source
from cplus_service.release.parser import (
    normalize,
    parse_prowlarr_result,
    parse_prowlarr_results,
    parse_title,
)

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Movie.2024.2160p.WEB-DL", "movie 2024 2160p web-dl"),
        ("Movie 2024 2160p WEB-DL", "movie 2024 2160p web-dl"),
        ("Movie_2024_2160p_WEB-DL", "movie 2024 2160p web-dl"),
        ("Movie.2024 2160p_WEB-DL", "movie 2024 2160p web-dl"),
        ("Movie...2024   2160p", "movie 2024 2160p"),
    ],
)
def test_normalize_folds_all_delimiters_to_one_form(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.H.265-FLUX", Resolution.UHD_2160P),
        ("Dune Part Two 2024 2160p WEB-DL DDP5 1 Atmos H 265-FLUX", Resolution.UHD_2160P),
        ("Dune.Part.Two.2024.4K.WEB-DL.H.265-FLUX", Resolution.UHD_2160P),
        ("Dune.Part.Two.2024.UHD.BluRay.x265-GROUP", Resolution.UHD_2160P),
        ("Sicario.2015.1080p.BluRay.x264-SPARKS", Resolution.FHD_1080P),
        ("Sicario 2015 1080p BluRay x264-SPARKS", Resolution.FHD_1080P),
        ("Sicario.2015.720p.BluRay.x264-YIFY", Resolution.HD_720P),
        ("Old.Movie.1998.480p.WEBRip.x264-GROUP", Resolution.SD_480P),
        ("Some.Movie.2024.WEB-DL.x264-GROUP", Resolution.UNKNOWN),
        ("Some.Movie.2024.3840x2160.WEB-DL-GROUP", Resolution.UHD_2160P),
    ],
)
def test_resolution(title: str, expected: Resolution) -> None:
    assert parse_title(title).resolution is expected


def test_resolution_prefers_the_highest_when_several_are_named() -> None:
    assert parse_title("Movie.2024.2160p.1080p.WEB-DL-GRP").resolution is Resolution.UHD_2160P


# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.H.265-FLUX", Source.WEB_DL),
        ("Movie 2024 2160p WEB-DL DDP5 1 Atmos H 265-FLUX", Source.WEB_DL),
        ("Movie.2024.2160p.WEBDL.H265-GRP", Source.WEB_DL),
        ("Movie.2024.1080p.WEB.H264-GRP", Source.WEB_DL),
        ("Movie.2024.1080p.WEBRip.x264-GRP", Source.WEBRIP),
        ("Movie 2024 1080p WEB-Rip x264-GRP", Source.WEBRIP),
        ("Movie.2024.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FraMeSToR", Source.REMUX),
        ("Movie 2024 2160p UHD BluRay Remux HEVC TrueHD 7 1 Atmos-FraMeSToR", Source.REMUX),
        ("Movie.2024.1080p.BluRay.x264-SPARKS", Source.BLURAY),
        ("Movie.2024.1080p.BDRip.x265-GRP", Source.BLURAY),
        ("Movie.2024.1080p.x265-GRP", Source.ENCODE),
        ("Movie.2024.1080p.HEVC-GRP", Source.ENCODE),
        ("Movie.2024.1080p.AV1-GRP", Source.ENCODE),
        ("Movie.2024.1080p.x266-GRP", Source.ENCODE),
        ("Movie.2024-GRP", Source.UNKNOWN),
    ],
)
def test_source(title: str, expected: Source) -> None:
    assert parse_title(title).source is expected


def test_encode_detection_covers_more_than_x264_and_x265() -> None:
    # The Swift implementation only knew x264/x265; these four must all read as
    # encodes rather than falling through to "untouched disc".
    for codec in ("x266", "HEVC", "AV1", "x265"):
        parsed = parse_title(f"Movie.2024.2160p.{codec}-GRP")
        assert parsed.source is Source.ENCODE, codec
        assert parsed.is_full_disc is False, codec


# --------------------------------------------------------------------------- #
# Full disc
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.COMPLETE.UHD.BLURAY-TERMiNAL",
        "Movie 2024 COMPLETE UHD BLURAY-TERMiNAL",
        "Movie.2024.2160p.UHD.Blu-ray.HEVC.TrueHD.7.1.Atmos.BDMV-GRP",
        "Movie.2024.1080p.BluRay.AVC.DTS-HD.MA.5.1-GRP",
        "Movie.2024.UHD.BD66.Blu-ray.Untouched-GRP",
        "Movie.2024.1080p.Blu-ray.VC-1.TrueHD.5.1-GRP",
    ],
)
def test_full_disc_detected(title: str) -> None:
    assert parse_title(title).is_full_disc is True


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
        "Movie.2024.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-FraMeSToR",
        "Movie.2024.1080p.BluRay.x264-SPARKS",
        "Movie.2024.1080p.BDRip.x265-GRP",
        "Movie.2024.1080p.WEBRip.x264-GRP",
    ],
)
def test_not_full_disc(title: str) -> None:
    assert parse_title(title).is_full_disc is False


def test_full_discs_are_dropped_at_the_parser_boundary() -> None:
    raws = [
        {"title": "Movie.2024.2160p.WEB-DL.HEVC-FLUX", "guid": "a", "indexerId": 1},
        {"title": "Movie.2024.COMPLETE.UHD.BLURAY-TERMiNAL", "guid": "b", "indexerId": 1},
        {"title": "Movie.2024.1080p.BluRay.x264-SPARKS", "guid": "c", "indexerId": 2},
    ]
    results = parse_prowlarr_results(raws)

    assert [r.guid for r in results] == ["a", "c"]
    assert all(r.is_full_disc is False for r in results)


# --------------------------------------------------------------------------- #
# HDR / Dolby Vision
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "hdr10plus", "hdr"),
    [
        ("Movie.2024.2160p.WEB-DL.HDR10Plus.HEVC-GRP", True, False),
        ("Movie.2024.2160p.WEB-DL.HDR10+.HEVC-GRP", True, False),
        ("Movie 2024 2160p WEB-DL HDR10+ HEVC-GRP", True, False),
        # HDR10P is the most common spelling in the wild.
        ("Movie.2024.2160p.WEB-DL.HDR10P.HEVC-GRP", True, False),
        ("Movie 2024 2160p WEB-DL HDR10P HEVC-GRP", True, False),
        ("Movie.2024.2160p.WEB-DL.hdr10p.HEVC-GRP", True, False),
        ("Movie.2024.2160p.WEB-DL.DV.HDR10P.HEVC-GRP", True, False),
        ("Movie.2024.2160p.WEB-DL.HDR10.HEVC-GRP", False, True),
        ("Movie.2024.2160p.WEB-DL.HDR.HEVC-GRP", False, True),
        ("Movie.2024.1080p.WEB-DL.H264-GRP", False, False),
    ],
)
def test_hdr_flags(title: str, hdr10plus: bool, hdr: bool) -> None:
    parsed = parse_title(title)
    assert parsed.is_hdr10plus is hdr10plus
    assert parsed.is_hdr is hdr


def test_hdr10plus_and_plain_hdr_are_mutually_exclusive_tags() -> None:
    parsed = parse_title("Movie.2024.2160p.WEB-DL.HDR10+.HEVC-GRP")
    assert parsed.hdr_tags == ["HDR10+"]


@pytest.mark.parametrize(
    "spelling", ["HDR10+", "HDR10P", "HDR10Plus", "HDRPlus", "HDR+", "hdr 10 +"]
)
def test_every_hdr10plus_spelling_produces_the_same_tag(spelling: str) -> None:
    parsed = parse_title(f"Movie.2024.2160p.WEB-DL.{spelling}.HEVC-GRP")
    assert parsed.is_hdr10plus is True
    assert parsed.is_hdr is False
    assert parsed.hdr_tags == ["HDR10+"]


def test_hdr10plus_long_form_is_not_clipped_to_the_short_one() -> None:
    # `hdr10p` must not swallow the `lus` of `hdr10plus` and leave it unmatched.
    assert parse_title("Movie.2024.2160p.WEB-DL.HDR10PLUS.HEVC-GRP").is_hdr10plus is True


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Explicit markers always win over the heuristic.
        ("Movie.2024.2160p.WEB-DL.DV.P5.HEVC-GRP", 5),
        ("Movie.2024.2160p.WEB-DL.DV.P8.HDR.HEVC-GRP", 8),
        ("Movie 2024 2160p WEB-DL DV P8 HDR HEVC-GRP", 8),
        ("Movie.2024.2160p.BluRay.REMUX.DV.P7.FEL.HEVC-GRP", 7),
        ("Movie.2024.2160p.WEB-DL.DVHE.05.HEVC-GRP", 5),
        ("Movie.2024.2160p.WEB-DL.DoVi.Profile.8.HEVC-GRP", 8),
        # FEL/MEL are both the dual-layer profile.
        ("Movie.2024.2160p.BluRay.DV.MEL.HEVC-GRP", 7),
        # Heuristic fallbacks.
        ("Movie.2024.2160p.UHD.BluRay.REMUX.DV.HEVC.TrueHD-GRP", 7),
        ("Movie.2024.2160p.BluRay.DV.HDR.x265-GRP", 8),
        ("Movie.2024.2160p.WEB-DL.DV.HDR.HEVC-GRP", 8),
        ("Movie.2024.2160p.WEB-DL.DV.DDP5.1-GRP", 5),
        # "Hybrid" on a REMUX means a converted single-layer profile 8 track...
        ("Movie.2024.2160p.UHD.BluRay.REMUX.DV.Hybrid.HEVC.TrueHD-GRP", 8),
        # ...unless HDR10+ is also present, which keeps it a profile 7 FEL track.
        ("Movie.2024.2160p.UHD.BluRay.REMUX.DV.Hybrid.HDR10+.HEVC.TrueHD-GRP", 7),
        # No DV at all.
        ("Movie.2024.2160p.WEB-DL.HDR.HEVC-GRP", 0),
    ],
)
def test_dv_profile(title: str, expected: int) -> None:
    assert parse_title(title).dv_profile == expected


def test_dv_tags_expose_both_the_precise_and_coarse_token() -> None:
    parsed = parse_title("Movie.2024.2160p.WEB-DL.DV.P8.HDR.HEVC-GRP")
    assert parsed.hdr_tags == ["DV_P8", "DV", "HDR10"]


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "atmos", "dtsx", "truehd"),
    [
        ("Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX", True, False, False),
        ("Movie 2024 2160p WEB-DL DDP5 1 Atmos HEVC-FLUX", True, False, False),
        ("Movie.2024.2160p.BluRay.REMUX.TrueHD.7.1.Atmos-FraMeSToR", True, False, True),
        ("Movie.2024.2160p.BluRay.REMUX.DTS-X.7.1-GRP", False, True, False),
        ("Movie.2024.2160p.BluRay.REMUX.DTS-HD.MA.5.1-GRP", False, False, False),
        ("Movie.2024.2160p.BluRay.REMUX.TrueHD.5.1-GRP", False, False, True),
        ("Movie 2024 2160p BluRay REMUX DTS:X 7 1 TrueHD Atmos-GRP", True, True, True),
        ("Movie.2024.2160p.BluRay.REMUX.DTSX.7.1-GRP", False, True, False),
    ],
)
def test_audio_flags(title: str, atmos: bool, dtsx: bool, truehd: bool) -> None:
    parsed = parse_title(title)
    assert parsed.has_atmos is atmos
    assert parsed.has_dtsx is dtsx
    assert parsed.has_truehd is truehd


def test_dts_hd_ma_is_not_mistaken_for_dts_x_or_truehd() -> None:
    parsed = parse_title("Movie.2024.1080p.BluRay.x264.DTS-HD.MA.5.1-GRP")
    assert parsed.has_dtsx is False
    assert parsed.has_truehd is False


def test_audio_formats_are_independent() -> None:
    parsed = parse_title("Movie.2024.2160p.BluRay.REMUX.TrueHD.7.1.Atmos.DTS-X-GRP")
    assert parsed.audio_tags == ["Atmos", "DTS:X", "TrueHD"]


# --------------------------------------------------------------------------- #
# REPACK / PROPER
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "flagged", "version"),
    [
        ("Movie.2024.REPACK.2160p.WEB-DL.HEVC-FLUX", True, 1),
        ("Movie 2024 REPACK 2160p WEB-DL HEVC-FLUX", True, 1),
        ("Movie.2024.REPACK2.2160p.WEB-DL.HEVC-FLUX", True, 2),
        ("Movie.2024.REPACK3.1080p.WEB-DL-GRP", True, 3),
        ("Movie.2024.PROPER.1080p.BluRay.x264-GRP", True, 1),
        ("Movie.2024.REAL.PROPER.1080p.BluRay.x264-GRP", True, 2),
        ("Movie.2024.2160p.WEB-DL.HEVC-FLUX", False, None),
    ],
)
def test_repack_and_proper(title: str, flagged: bool, version: int | None) -> None:
    parsed = parse_title(title)
    assert parsed.is_repack_or_proper is flagged
    assert parsed.repack_version == version


def test_repack_and_base_release_share_a_base_title() -> None:
    base = parse_title("Movie.Name.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX")
    repack = parse_title("Movie.Name.2024.REPACK.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX")
    spaced = parse_title("Movie Name 2024 REPACK 2160p WEB-DL DDP5 1 Atmos HEVC-FLUX")

    assert base.base_title == "movie name 2024"
    assert repack.base_title == base.base_title
    assert spaced.base_title == base.base_title


def test_different_movies_do_not_share_a_base_title() -> None:
    a = parse_title("Movie.One.2024.2160p.WEB-DL-GRP")
    b = parse_title("Movie.Two.2024.2160p.WEB-DL-GRP")
    assert a.base_title != b.base_title


# --------------------------------------------------------------------------- #
# Pre-release
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.HDCAM.1080p.x264-GRP",
        "Movie 2024 CAM x264-GRP",
        "Movie.2024.CAMRip.XviD-GRP",
        "Movie.2024.TS.1080p.x264-GRP",
        "Movie.2024.HDTS.720p.x264-GRP",
        "Movie.2024.TELESYNC.x264-GRP",
        "Movie.2024.TELECINE.1080p-GRP",
        "Movie.2024.DVDSCR.XviD-GRP",
        "Movie.2024.SCREENER.x264-GRP",
        "Movie.2024.R5.LiNE.XviD-GRP",
        "Movie.2024.WORKPRINT.x264-GRP",
        "Movie.2024.DCP.1080p.x264-GRP",
        # Newer `*Rip` spellings.
        "Movie.2024.HDRip.1080p.x264-GRP",
        "Movie 2024 HDRip 1080p x264-GRP",
        "Movie.2024.HD-Rip.1080p.x264-GRP",
        "Movie.2024.DCPRip.1080p.x264-GRP",
        "Movie.2024.DCP-Rip.1080p.x264-GRP",
        "Movie.2024.CAMRip.1080p.x264-GRP",
        "Movie.2024.HDTS.1080p.x264-GRP",
        "Movie.2024.HDTC.1080p.x264-GRP",
    ],
)
def test_prerelease_detected(title: str) -> None:
    assert parse_title(title).is_prerelease is True


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.HDRip.1080p.x264-GRP",
        "Movie.2024.DCPRip.1080p.x264-GRP",
        "Movie.2024.CAMRip.XviD-GRP",
    ],
)
def test_a_prerelease_rip_is_still_an_encode_not_a_full_disc(title: str) -> None:
    # The two flags answer different questions, and a release carries both.
    parsed = parse_title(title)
    assert parsed.is_prerelease is True
    assert parsed.is_full_disc is False


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.1080p.BDRip.x265-GRP",
        "Movie.2024.1080p.BRRip.x264-GRP",
        "Movie.2024.1080p.WEBRip.x264-GRP",
        "Movie.2024.1080p.DVDRip.XviD-GRP",
    ],
)
def test_other_rip_tags_are_not_prereleases(title: str) -> None:
    # Only the pre-release *Rip spellings count; ordinary source rips do not.
    assert parse_title(title).is_prerelease is False


@pytest.mark.parametrize(
    "title",
    [
        "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
        "Movie.2024.1080p.BluRay.x264.DTS-HD.MA.5.1-GRP",
    ],
)
def test_prerelease_not_detected(title: str) -> None:
    assert parse_title(title).is_prerelease is False


def test_group_named_ts_is_not_read_as_a_telesync() -> None:
    # The trailing -GROUP segment is excluded from tag matching precisely so a
    # group name cannot masquerade as a quality tag.
    parsed = parse_title("Movie.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-TS")
    assert parsed.release_group == "TS"
    assert parsed.is_prerelease is False


# --------------------------------------------------------------------------- #
# Release group
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX", "FLUX"),
        ("Movie 2024 2160p WEB-DL DDP5 1 Atmos HEVC-FLUX", "FLUX"),
        ("Movie 2024 2160p WEB-DL DDP5 1 Atmos HEVC - FLUX", "FLUX"),
        ("Movie.2024.1080p.BluRay.x264-SPARKS.mkv", "SPARKS"),
        ("Movie.2024.1080p.BluRay.x264-RARBG[rarbg]", "RARBG"),
        ("Movie.2024.2160p.UHD.BluRay.REMUX.HEVC-FraMeSToR", "FraMeSToR"),
        ("Movie.2024.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-D-Z0N3", "D-Z0N3"),
        ("[GroupName] Movie 2024 2160p WEB-DL HEVC", "GroupName"),
        ("Movie.2024.2160p.WEB-DL", None),
        ("Spider-Man.2002.1080p.BluRay.x264", None),
    ],
)
def test_release_group(title: str, expected: str | None) -> None:
    assert parse_title(title).release_group == expected


def test_hyphenated_movie_name_does_not_leak_into_the_group() -> None:
    parsed = parse_title("Spider-Man.No.Way.Home.2021.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX")
    assert parsed.release_group == "FLUX"
    assert parsed.base_title == "spider man no way home 2021"


# --------------------------------------------------------------------------- #
# Prowlarr passthrough
# --------------------------------------------------------------------------- #


def test_prowlarr_fields_pass_through_untouched() -> None:
    raw = {
        "title": "Movie.2024.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX",
        "guid": "https://indexer.example/details/abc123",
        "indexerId": 7,
        "indexer": "Example Tracker",
        "size": 25_000_000_000,
        "publishDate": "2024-05-01T12:00:00Z",
        "seeders": 42,
        "leechers": 3,
        "downloadUrl": "https://indexer.example/dl/abc123",
        "infoUrl": "https://indexer.example/info/abc123",
        "protocol": "torrent",
    }
    parsed = parse_prowlarr_result(raw)

    assert parsed.title == raw["title"]
    assert parsed.guid == raw["guid"]
    assert parsed.indexer_id == 7
    assert parsed.indexer == "Example Tracker"
    assert parsed.size_bytes == 25_000_000_000
    assert parsed.publish_date is not None
    assert parsed.seeders == 42
    assert parsed.leechers == 3
    assert parsed.protocol == "torrent"
    assert parsed.size_gb == pytest.approx(23.28, abs=0.01)


def test_missing_prowlarr_fields_are_tolerated() -> None:
    parsed = parse_prowlarr_result({"title": "Movie.2024.1080p.WEB-DL-GRP"})
    assert parsed.guid == ""
    assert parsed.indexer_id is None
    assert parsed.size_bytes is None
    assert parsed.size_gb is None


def test_realistic_mixed_delimiter_title_parses_end_to_end() -> None:
    parsed = parse_title(
        "Dune.Part Two.2024.REPACK.2160p.UHD.BluRay.REMUX.DV.P7.FEL.HDR10+."
        "HEVC.TrueHD.7.1.Atmos.DTS-X-FraMeSToR"
    )

    assert parsed.resolution is Resolution.UHD_2160P
    assert parsed.source is Source.REMUX
    assert parsed.dv_profile == 7
    assert parsed.is_hdr10plus is True
    assert parsed.is_hdr is False
    assert parsed.has_atmos is True
    assert parsed.has_dtsx is True
    assert parsed.has_truehd is True
    assert parsed.is_repack_or_proper is True
    assert parsed.repack_version == 1
    assert parsed.is_prerelease is False
    assert parsed.is_full_disc is False
    assert parsed.release_group == "FraMeSToR"
    assert parsed.base_title == "dune part two 2024"
