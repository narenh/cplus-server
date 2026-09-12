"""``cplus_service.search.categorize`` — the admin search's own categorisation.

This is the one place the service buckets, sorts and tags releases itself
rather than leaving it to a client; see the module docstring for why it's
scoped to ``GET /manager/search`` alone.
"""

from __future__ import annotations

from datetime import datetime

from cplus_service.release.models import ParsedRelease, Resolution
from cplus_service.search.categorize import (
    CATEGORY_ORDER,
    categorize_releases,
    category_of,
    release_tags,
)


def release(**overrides: object) -> ParsedRelease:
    defaults: dict[str, object] = {"title": "Movie.2024.1080p.WEB-DL-GRP", "guid": "g"}
    defaults.update(overrides)
    return ParsedRelease(**defaults)


# --------------------------------------------------------------------------- #
# category_of
# --------------------------------------------------------------------------- #


def test_2160p_with_dolby_vision_is_4k_dv() -> None:
    r = release(resolution=Resolution.UHD_2160P, dv_profile=8)
    assert category_of(r) == "4k_dv"


def test_2160p_without_dolby_vision_is_4k() -> None:
    r = release(resolution=Resolution.UHD_2160P, dv_profile=0, is_hdr=True)
    assert category_of(r) == "4k"


def test_1080p_is_hd1080() -> None:
    r = release(resolution=Resolution.FHD_1080P)
    assert category_of(r) == "hd1080"


def test_720p_is_other() -> None:
    r = release(resolution=Resolution.HD_720P)
    assert category_of(r) == "other"


def test_unknown_resolution_is_other() -> None:
    r = release(resolution=Resolution.UNKNOWN)
    assert category_of(r) == "other"


def test_prerelease_wins_over_resolution() -> None:
    """A CAM of a 2160p DV film is still a CAM, not a 4K DV release."""
    r = release(resolution=Resolution.UHD_2160P, dv_profile=8, is_prerelease=True)
    assert category_of(r) == "prerelease"


# --------------------------------------------------------------------------- #
# release_tags
# --------------------------------------------------------------------------- #


def test_tags_cover_dv_profile_hdr_and_audio() -> None:
    r = release(
        dv_profile=5,
        is_hdr10plus=False,
        is_hdr=False,
        has_atmos=True,
        has_dtsx=True,
    )
    assert release_tags(r) == ["dv5", "Atmos", "dtsx"]


def test_hdr10plus_tag_is_lowercase_hdr10p() -> None:
    r = release(is_hdr10plus=True)
    assert release_tags(r) == ["hdr10p"]


def test_plain_hdr10_tag() -> None:
    r = release(is_hdr=True)
    assert release_tags(r) == ["HDR10"]


def test_no_tags_when_nothing_matches() -> None:
    r = release()
    assert release_tags(r) == []


def test_dv_profile_8_tag() -> None:
    r = release(dv_profile=8)
    assert release_tags(r) == ["dv8"]


# --------------------------------------------------------------------------- #
# categorize_releases
# --------------------------------------------------------------------------- #


def test_every_category_is_present_even_when_empty() -> None:
    result = categorize_releases([])
    assert [c["id"] for c in result] == list(CATEGORY_ORDER)
    assert all(c["releases"] == [] for c in result)


def test_releases_sort_largest_first_within_a_category() -> None:
    small = release(guid="small", resolution=Resolution.FHD_1080P, size_bytes=1_000)
    big = release(guid="big", resolution=Resolution.FHD_1080P, size_bytes=9_000)
    unknown = release(guid="unknown", resolution=Resolution.FHD_1080P, size_bytes=None)

    result = categorize_releases([small, unknown, big])
    hd1080 = next(c["releases"] for c in result if c["id"] == "hd1080")
    assert [r["guid"] for r in hd1080] == ["big", "small", "unknown"]


def test_prerelease_sorts_newest_first_by_publish_date_not_size() -> None:
    older = release(
        guid="older",
        is_prerelease=True,
        size_bytes=9_000,
        publish_date=datetime(2023, 1, 1),
    )
    newer = release(
        guid="newer",
        is_prerelease=True,
        size_bytes=1_000,
        publish_date=datetime(2024, 1, 1),
    )
    undated = release(guid="undated", is_prerelease=True, size_bytes=5_000, publish_date=None)

    result = categorize_releases([older, undated, newer])
    prerelease = next(c["releases"] for c in result if c["id"] == "prerelease")
    # Newer wins despite being the smallest file; undated sorts last.
    assert [r["guid"] for r in prerelease] == ["newer", "older", "undated"]


def test_each_release_payload_carries_its_tags() -> None:
    r = release(resolution=Resolution.FHD_1080P, has_atmos=True)
    result = categorize_releases([r])
    hd1080 = next(c["releases"] for c in result if c["id"] == "hd1080")
    assert hd1080[0]["tags"] == ["Atmos"]


def test_releases_land_in_distinct_categories() -> None:
    dv = release(guid="dv", resolution=Resolution.UHD_2160P, dv_profile=7)
    plain_4k = release(guid="plain-4k", resolution=Resolution.UHD_2160P)
    hd = release(guid="hd", resolution=Resolution.FHD_1080P)
    cam = release(guid="cam", is_prerelease=True)
    sd = release(guid="sd", resolution=Resolution.SD_480P)

    result = categorize_releases([dv, plain_4k, hd, cam, sd])
    by_id = {c["id"]: [r["guid"] for r in c["releases"]] for c in result}
    assert by_id == {
        "4k_dv": ["dv"],
        "4k": ["plain-4k"],
        "hd1080": ["hd"],
        "prerelease": ["cam"],
        "other": ["sd"],
    }
