"""Quality profile rule engine.

Pure: no I/O, no database access, no clock.  Give it a candidate list and a
profile, get back the recommended release or ``None``.

The engine deliberately knows nothing about the global preferred-indexer hard
filter.  That filter is not a profile rule — it applies unconditionally to every
profile, and the caller (stage 2's search endpoint) is responsible for deciding
the effective candidate set before calling in here.  See
:func:`preferred_indexer_candidates` for the one helper this module offers
toward that, which the caller may use but the engine never invokes itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..release.models import BYTES_PER_GB, ParsedRelease
from .models import (
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
)

# Sorts after every explicitly listed value in an ordered preference rule.
_UNRANKED = 1_000_000


def preferred_indexer_candidates(
    candidates: Sequence[ParsedRelease], preferred_indexer_id: int | None
) -> list[ParsedRelease]:
    """Apply the global preferred-indexer hard filter.

    Not a profile rule: it runs once, before any profile's rules, identically
    for every profile, and there is no per-profile toggle for it.

    * ``preferred_indexer_id is None`` ("All indexers") — no restriction.
    * Set, and that indexer returned something — restrict to it.
    * Set, but that indexer returned nothing — fall back to the full set rather
      than returning no recommendation.
    """
    if preferred_indexer_id is None:
        return list(candidates)
    preferred = [c for c in candidates if c.indexer_id == preferred_indexer_id]
    return preferred or list(candidates)


def apply_filters(
    candidates: Sequence[ParsedRelease], profile: QualityProfile
) -> list[ParsedRelease]:
    """Drop every candidate eliminated by the profile's filter rules.

    Full discs are dropped here too, defensively — the parser already excludes
    them, so this only matters for hand-built candidate lists.
    """
    survivors = [c for c in candidates if not c.is_full_disc]

    for rule in profile.filters:
        if isinstance(rule, ExcludePrereleaseRule):
            if rule.enabled:
                survivors = [c for c in survivors if not c.is_prerelease]
        elif isinstance(rule, KeywordExcludeRule):
            keywords = [k.lower() for k in rule.values if k.strip()]
            if keywords:
                survivors = [
                    c for c in survivors if not any(k in c.title.lower() for k in keywords)
                ]
        elif isinstance(rule, SizeCapGbRule):
            cap = rule.value * BYTES_PER_GB
            survivors = [
                c for c in survivors if c.size_bytes is None or c.size_bytes <= cap
            ]

    return survivors


def _superseded_base_titles(candidates: Sequence[ParsedRelease]) -> set[str]:
    """Base titles for which a REPACK/PROPER is present in the candidate set."""
    return {c.base_title for c in candidates if c.is_repack_or_proper and c.base_title}


def _index_of_best(tags: Sequence[str], values: Sequence[str]) -> int:
    ranks = [values.index(tag) for tag in tags if tag in values]
    return min(ranks) if ranks else _UNRANKED


def _size_key(release: ParsedRelease, rule: SizeRule) -> tuple[int, float]:
    """``(tier, value)`` — lower is better.

    Tier 0 is at-or-under the cap, tier 1 is over it, tier 2 is unknown size.
    Over-cap releases are ordered smallest-first regardless of ``direction``,
    since the smallest is the one closest to the cap.
    """
    if release.size_bytes is None:
        return (2, 0.0)

    size = float(release.size_bytes)
    if rule.cap_gb is not None and size > rule.cap_gb * BYTES_PER_GB:
        return (1, size)
    if rule.direction is SizeDirection.LARGEST:
        return (0, -size)
    return (0, size)


def _preference_key(
    release: ParsedRelease, profile: QualityProfile, superseded: set[str]
) -> tuple:
    """Lexicographic sort key built from the profile's preference rules.

    Rules contribute in the order they appear in the profile, so the first
    preference rule decides and each later one breaks the ties left by the
    previous.
    """
    key: list[tuple] = []

    for rule in profile.preferences:
        if isinstance(rule, RepackProperPriorityRule):
            if not rule.enabled:
                key.append((0,))
            elif release.is_repack_or_proper or release.base_title not in superseded:
                key.append((0,))
            else:
                key.append((1,))
        elif isinstance(rule, ResolutionOrderRule):
            values = list(rule.values)
            rank = values.index(release.resolution) if release.resolution in values else _UNRANKED
            key.append((rank,))
        elif isinstance(rule, SourceOrderRule):
            values = list(rule.values)
            rank = values.index(release.source) if release.source in values else _UNRANKED
            key.append((rank,))
        elif isinstance(rule, HdrMatchRule):
            key.append((_index_of_best(release.hdr_tags, rule.values),))
        elif isinstance(rule, AudioMatchRule):
            key.append((_index_of_best(release.audio_tags, list(rule.values)),))
        elif isinstance(rule, SizeRule):
            key.append(_size_key(release, rule))

    return tuple(key)


def rank(
    candidates: Sequence[ParsedRelease], profile: QualityProfile
) -> list[ParsedRelease]:
    """Filtered candidates ordered best-first.

    Ties that survive every rule keep their original (Prowlarr) order.  Useful
    for debugging a profile and for an admin "why this release?" view; callers
    that just want the pick should use :func:`recommend`.
    """
    survivors = apply_filters(candidates, profile)
    superseded = _superseded_base_titles(survivors)
    decorated = [
        (_preference_key(release, profile, superseded), index, release)
        for index, release in enumerate(survivors)
    ]
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [release for _, _, release in decorated]


def recommend(
    candidates: Sequence[ParsedRelease], profile: QualityProfile
) -> ParsedRelease | None:
    """Return the recommended release for ``profile``, or ``None``.

    ``None`` is an expected, valid outcome — it means every candidate was
    eliminated by the profile's filter rules (or the candidate list was empty).
    It is not an error.
    """
    survivors = apply_filters(candidates, profile)
    if not survivors:
        return None
    superseded = _superseded_base_titles(survivors)
    _, best = min(
        enumerate(survivors),
        key=lambda pair: (_preference_key(pair[1], profile, superseded), pair[0]),
    )
    return best
