"""Quality profile rule engine.

Pure: no I/O, no database access, no clock.  Give it a candidate list and a
profile, get back the recommended release or ``None``.

Three passes, in this order: filters eliminate, choices bucket what is left
into "I'd rather have this kind" rungs, and the preference rules order the
releases inside a rung.  :func:`explain` reports all three for every candidate
and is what the admin UI's preview is built on — the rules cannot be unclear
about what they are doing if the page can show it.

The engine deliberately knows nothing about the global preferred-indexer hard
filter.  That filter is not a profile rule — it applies unconditionally to every
profile, and the caller (stage 2's search endpoint) is responsible for deciding
the effective candidate set before calling in here.  See
:func:`preferred_indexer_candidates` for the one helper this module offers
toward that, which the caller may use but the engine never invokes itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC

from ..release.models import BYTES_PER_GB, ParsedRelease
from .models import (
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


def rejection(release: ParsedRelease, profile: QualityProfile) -> str | None:
    """Why this profile drops ``release``, in words, or ``None`` if it keeps it.

    The single source of truth for what a filter does: :func:`apply_filters` is
    this function applied to a list, and the admin UI's preview prints the
    reason verbatim.  Keeping the two together is what stops the explanation
    from drifting away from the behaviour it claims to explain.
    """
    if release.is_full_disc:
        return "full disc"

    for rule in profile.filters:
        if isinstance(rule, ExcludePrereleaseRule):
            if rule.enabled and release.is_prerelease:
                return "pre-release"
        elif isinstance(rule, KeywordExcludeRule):
            lowered = release.title.lower()
            for keyword in rule.values:
                clean = keyword.strip()
                if clean and clean.lower() in lowered:
                    return f"title contains {clean!r}"
        elif isinstance(rule, SizeCapGbRule):
            if release.size_bytes is not None and release.size_bytes > rule.value * BYTES_PER_GB:
                return f"larger than {_gb(rule.value)}"

    return None


def apply_filters(
    candidates: Sequence[ParsedRelease], profile: QualityProfile
) -> list[ParsedRelease]:
    """Drop every candidate eliminated by the profile's filter rules.

    Full discs are dropped here too, defensively — the parser already excludes
    them, so this only matters for hand-built candidate lists.
    """
    return [c for c in candidates if rejection(c, profile) is None]


def _gb(value: float) -> str:
    """A GB number as an admin would write it: ``15 GB``, not ``15.0 GB``."""
    return f"{value:g} GB"


# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


def matches(release: ParsedRelease, match: ChoiceMatch) -> bool:
    """Whether ``release`` is the kind of release ``match`` describes.

    An empty field is no constraint. A size bound is never satisfied by an
    unknown size — see :class:`~cplus_service.quality.models.ChoiceMatch`.
    """
    if match.resolutions and release.resolution not in match.resolutions:
        return False
    if match.sources and release.source not in match.sources:
        return False
    if match.hdr and not any(tag in match.hdr for tag in release.hdr_tags):
        return False
    if match.audio and not any(tag in match.audio for tag in release.audio_tags):
        return False

    if match.min_size_gb is not None or match.max_size_gb is not None:
        if release.size_bytes is None:
            return False
        size_gb = release.size_bytes / BYTES_PER_GB
        if match.min_size_gb is not None and size_gb < match.min_size_gb:
            return False
        if match.max_size_gb is not None and size_gb > match.max_size_gb:
            return False

    return True


def choice_index(release: ParsedRelease, profile: QualityProfile) -> int:
    """Which choice this release lands in — lower is better.

    Returns ``len(profile.choices)`` for a release matching none of them: it
    ranks after everything that matched one, but it is still eligible. A
    profile with no choices puts everything in the same (zeroth) bucket, which
    is why choices are free to be absent.
    """
    for index, choice in enumerate(profile.choices):
        if matches(release, choice.match):
            return index
    return len(profile.choices)


def _tie_break_key(release: ParsedRelease, choice: Choice) -> tuple:
    """Ordering *within* one choice — lower is better, ``()`` for no override.

    Every branch pushes a release the tie-break cannot speak about (no size, no
    date, no seeder count) behind the ones it can, rather than treating a
    missing value as zero — an indexer that reports nothing should not win or
    lose the pick on that account.
    """
    if choice.tie_break is None:
        return ()

    if choice.tie_break is TieBreak.NEWEST:
        if release.publish_date is None:
            return (1, 0.0)
        published = release.publish_date
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return (0, -published.timestamp())

    if choice.tie_break is TieBreak.MOST_SEEDERS:
        if release.seeders is None:
            return (1, 0.0)
        return (0, -float(release.seeders))

    if release.size_bytes is None:
        return (1, 0.0)
    size_gb = release.size_bytes / BYTES_PER_GB
    if choice.tie_break is TieBreak.SMALLEST:
        return (0, size_gb)
    if choice.tie_break is TieBreak.CLOSEST_TO_GB:
        target = choice.tie_break_gb
        if target is None:
            # A target nobody set is not an ordering; fall back to biggest
            # rather than pretending every release is equidistant from nothing.
            return (0, -size_gb)
        return (0, abs(size_gb - target))
    return (0, -size_gb)


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
    """Lexicographic sort key — lower is better.

    Read left to right, it is the profile in one line:

    1. **which choice** the release matched. Nothing below can promote a
       second-choice release above a first-choice one;
    2. that choice's **tie-break**, if it set one;
    3. the profile's **preference rules**, in profile order — the first
       decides, each later one breaks the ties left by the previous.
    """
    index = choice_index(release, profile)
    key: list[tuple] = [(index,)]
    if index < len(profile.choices):
        key.append(_tie_break_key(release, profile.choices[index]))

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


# --------------------------------------------------------------------------- #
# Explaining a profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Judgement:
    """What a profile did to one release, and why.

    Built for the admin UI's preview, which is the whole answer to "it is not
    clear what these rules are doing": every candidate is shown either in its
    ranked position with the choice it landed in, or greyed out with the filter
    that dropped it.
    """

    release: ParsedRelease
    dropped_by: str | None = None
    #: Index into ``profile.choices``, or ``None`` when the release matched no
    #: choice (or the profile has none). Meaningless for a dropped release.
    choice: int | None = None
    #: Position among the survivors, best first, or ``None`` if dropped.
    position: int | None = None

    @property
    def kept(self) -> bool:
        return self.dropped_by is None


def explain(
    candidates: Sequence[ParsedRelease], profile: QualityProfile
) -> list[Judgement]:
    """Every candidate, judged: survivors ranked best-first, then the dropped.

    The dropped keep their original order — there is no ranking among releases
    the profile refused, and re-ordering them would imply one.
    """
    kept: list[ParsedRelease] = []
    dropped: list[Judgement] = []
    for release in candidates:
        reason = rejection(release, profile)
        if reason is None:
            kept.append(release)
        else:
            dropped.append(Judgement(release=release, dropped_by=reason))

    ranked = rank(kept, profile)
    judged = [
        Judgement(
            release=release,
            choice=(
                index if (index := choice_index(release, profile)) < len(profile.choices)
                else None
            ),
            position=position,
        )
        for position, release in enumerate(ranked)
    ]
    return judged + dropped
