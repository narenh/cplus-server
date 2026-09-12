"""Categorise, sort and tag releases for the admin's unrestricted search.

``GET /manager/search`` is the one place this service does its own
categorisation of Prowlarr results — deliberately, and deliberately *not* by
adding a ``category`` field to :class:`~cplus_service.release.models.ParsedRelease`
itself. That model is shared with tvOS, where sectioning stays a client-side
concern driven off the parsed tags (see the module docstring there). The admin
app's free-text/IMDB-id search has no client-side categorisation at all today,
Prowlarr fans out to every indexer, and admins were left to make sense of a
flat, unsorted list themselves — this module is what replaces that.

Five categories, always present in this order so the client can render fixed
sections without checking first:

* ``4k_dv`` — 2160p with any Dolby Vision profile
* ``4k`` — 2160p, no Dolby Vision (HDR10/HDR10+/SDR)
* ``hd1080`` — 1080p
* ``prerelease`` — CAM/TS/telesync/screener/DCP/etc., whatever the resolution
* ``other`` — everything else (720p, 480p, unparsed)

Pre-release status is checked first and wins over resolution: a CAM of a
2160p film is still a CAM, not a 4K release worth offering as one.

Every category sorts by size, largest first — the same "biggest is the least
compressed copy" reasoning the quality engine's default profile uses — except
``prerelease``, which sorts by publish date, newest first: size says nothing
useful about a CAM, but a fresher rip replacing an earlier one does. A release
missing the sort key (no size, no date) sorts last within its category rather
than first, so an indexer that reports nothing never wins a slot on that
account.

Each release is also handed a client-facing ``tags`` list — ``dv5``/``dv7``/
``dv8``, ``HDR10``, ``hdr10p``, ``Atmos``, ``dtsx`` — for the admin UI to draw
as badges. This is a distinct, deliberately terser vocabulary from the
``hdr_tags``/``audio_tags`` computed fields already on the model (``DV_P8``,
``HDR10+``, ``DTS:X``, ...), which stay as they are for the quality engine.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from ..release.models import ParsedRelease, Resolution

CATEGORY_4K_DV = "4k_dv"
CATEGORY_4K = "4k"
CATEGORY_HD1080 = "hd1080"
CATEGORY_PRERELEASE = "prerelease"
CATEGORY_OTHER = "other"

#: Display/response order. Every category appears in the response in this
#: order, even when empty.
CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_4K_DV,
    CATEGORY_4K,
    CATEGORY_HD1080,
    CATEGORY_PRERELEASE,
    CATEGORY_OTHER,
)


def category_of(release: ParsedRelease) -> str:
    """Which of :data:`CATEGORY_ORDER` ``release`` belongs to."""
    if release.is_prerelease:
        return CATEGORY_PRERELEASE
    if release.resolution is Resolution.UHD_2160P:
        return CATEGORY_4K_DV if release.dv_profile else CATEGORY_4K
    if release.resolution is Resolution.FHD_1080P:
        return CATEGORY_HD1080
    return CATEGORY_OTHER


def release_tags(release: ParsedRelease) -> list[str]:
    """The admin UI's badge vocabulary: ``dv<profile>``, HDR10, hdr10p, Atmos, dtsx."""
    tags: list[str] = []
    if release.dv_profile:
        tags.append(f"dv{release.dv_profile}")
    if release.is_hdr10plus:
        tags.append("hdr10p")
    if release.is_hdr:
        tags.append("HDR10")
    if release.has_atmos:
        tags.append("Atmos")
    if release.has_dtsx:
        tags.append("dtsx")
    return tags


def _size_key(release: ParsedRelease) -> tuple[int, float]:
    """Largest first; unknown size sorts last."""
    if release.size_bytes is None:
        return (1, 0.0)
    return (0, -float(release.size_bytes))


def _date_key(release: ParsedRelease) -> tuple[int, float]:
    """Newest first; unknown publish date sorts last."""
    if release.publish_date is None:
        return (1, 0.0)
    published = release.publish_date
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return (0, -published.timestamp())


def _release_payload(release: ParsedRelease) -> dict[str, Any]:
    payload = release.model_dump(mode="json")
    payload["tags"] = release_tags(release)
    return payload


def categorize_releases(releases: Sequence[ParsedRelease]) -> list[dict[str, Any]]:
    """Group ``releases`` into categories, sort each, and tag every release.

    Returns a list ordered per :data:`CATEGORY_ORDER`, one entry per category:
    ``{"id": ..., "releases": [...]}``. Every category is present even when its
    list is empty.
    """
    buckets: dict[str, list[ParsedRelease]] = {category: [] for category in CATEGORY_ORDER}
    for release in releases:
        buckets[category_of(release)].append(release)

    result: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        bucket = buckets[category]
        key = _date_key if category == CATEGORY_PRERELEASE else _size_key
        bucket.sort(key=key)
        result.append(
            {
                "id": category,
                "releases": [_release_payload(release) for release in bucket],
            }
        )
    return result
