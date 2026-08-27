"""Quality profile schema.

A profile decides one thing: given every release an indexer returned for a
title, which one should this action grab?  It answers that in three parts, and
they are deliberately separate because they answer different questions.

**Filters — "never grab this."**  A filter eliminates a candidate outright,
before any ranking happens.  Position in the list is irrelevant: a filter
anywhere applies to everything.  These live in :attr:`QualityProfile.rules`
alongside the preference rules and are told apart by type.

**Choices — "I'd rather have this kind of release than that kind."**  An
ordered list of :class:`Choice`, each describing a *kind* of release.  Every
release matching the first choice beats every release matching the second,
whatever the preference rules say; a release matching none of them ranks last
but is still eligible.  This is the part that can express "the highest-bitrate
4K WEB copy, or failing that the biggest 1080p encode under 15 GB" — two
different wants with different size rules, which a single ordered preference
list cannot say.  A profile with no choices behaves exactly as it did before
they existed: one undifferentiated pool ranked by the preference rules.

**Preference rules — "when two releases are equally good, prefer…"**  These
rank whatever survives the filters, *within* a choice.  Their position **is**
load-bearing: the first decides, the second breaks its ties, and so on.  A
choice may override them with a tie-break of its own, which then decides and
leaves the preference rules to break *its* ties.

Three things involve a GB number and they are deliberately distinct concepts:

``size_cap_gb`` (filter)
    Drops anything larger than the cap outright.  A release over the cap can
    never be recommended.

``Choice.match.max_size_gb`` (choice)
    Part of a condition, not a filter: an over-cap release simply does not
    match *this* choice and falls through to a later one.  Nothing is dropped.

``size`` (preference)
    Final tie-break among otherwise-equal candidates.  Its optional ``cap_gb``
    only *demotes* over-cap releases behind under-cap ones; it never eliminates
    them, so an over-cap release still wins if it is the only thing left.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..release.models import AudioTag, Resolution, Source

# ``DV`` / ``DV_P8`` / ``HDR10+`` / ``HDR10`` / ``SDR``
_HDR_TOKEN = r"^(?:DV(?:_P\d{1,2})?|HDR10\+|HDR10|SDR)$"


class RuleType(StrEnum):
    # Filters
    EXCLUDE_PRERELEASE = "exclude_prerelease"
    KEYWORD_EXCLUDE = "keyword_exclude"
    SIZE_CAP_GB = "size_cap_gb"
    # Preferences
    REPACK_PROPER_PRIORITY = "repack_proper_priority"
    RESOLUTION_ORDER = "resolution_order"
    SOURCE_ORDER = "source_order"
    HDR_MATCH = "hdr_match"
    AUDIO_MATCH = "audio_match"
    SIZE = "size"


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Filter rules
# --------------------------------------------------------------------------- #


class ExcludePrereleaseRule(_Rule):
    """Drop CAM/TS/telecine/screener/DCP releases.  Off by default, i.e. absent
    from the profile; include it with ``enabled=False`` to record an explicit
    "off" without deleting the rule from the admin UI."""

    type: Literal[RuleType.EXCLUDE_PRERELEASE] = RuleType.EXCLUDE_PRERELEASE
    enabled: bool = True


class KeywordExcludeRule(_Rule):
    """Drop releases whose raw title contains any of these, case-insensitively."""

    type: Literal[RuleType.KEYWORD_EXCLUDE] = RuleType.KEYWORD_EXCLUDE
    values: list[str] = Field(default_factory=list)


class SizeCapGbRule(_Rule):
    """Drop releases larger than ``value`` GiB.

    Releases whose size Prowlarr did not report are kept — an unknown size is
    not evidence of a violation.
    """

    type: Literal[RuleType.SIZE_CAP_GB] = RuleType.SIZE_CAP_GB
    value: float = Field(gt=0)


# --------------------------------------------------------------------------- #
# Preference rules
# --------------------------------------------------------------------------- #


class RepackProperPriorityRule(_Rule):
    """Prefer a REPACK/PROPER over the base release of the same underlying title.

    Title-diffed, not tag-matched: a REPACK only demotes the base releases that
    share its :attr:`~cplus_service.release.models.ParsedTitle.base_title`.  A
    release with no REPACK sibling in the candidate set is not penalised.
    """

    type: Literal[RuleType.REPACK_PROPER_PRIORITY] = RuleType.REPACK_PROPER_PRIORITY
    enabled: bool = True


class ResolutionOrderRule(_Rule):
    """Preference order over resolutions.  Unlisted resolutions rank last but
    are **not** filtered out."""

    type: Literal[RuleType.RESOLUTION_ORDER] = RuleType.RESOLUTION_ORDER
    values: list[Resolution] = Field(default_factory=list)


class SourceOrderRule(_Rule):
    """Preference order over sources.  Unlisted sources rank last."""

    type: Literal[RuleType.SOURCE_ORDER] = RuleType.SOURCE_ORDER
    values: list[Source] = Field(default_factory=list)


class HdrMatchRule(_Rule):
    """Preference order over HDR/DV tags.

    Accepts ``DV``, ``DV_P5``/``DV_P7``/``DV_P8`` (or any ``DV_P<n>``),
    ``HDR10+``, ``HDR10`` and ``SDR``.  A release is scored by the best-ranked
    tag it carries, so a DV profile 8 release matches both ``DV_P8`` and the
    coarser ``DV``.
    """

    type: Literal[RuleType.HDR_MATCH] = RuleType.HDR_MATCH
    values: list[str] = Field(default_factory=list)

    @field_validator("values")
    @classmethod
    def _check_tokens(cls, values: list[str]) -> list[str]:
        bad = [v for v in values if not re.match(_HDR_TOKEN, v)]
        if bad:
            raise ValueError(f"unknown HDR tokens: {bad}")
        return values


class AudioMatchRule(_Rule):
    """Preference order over audio tags.  Atmos, DTS:X and TrueHD are distinct
    values and a release may carry any combination of them."""

    type: Literal[RuleType.AUDIO_MATCH] = RuleType.AUDIO_MATCH
    values: list[AudioTag] = Field(default_factory=list)


class SizeDirection(StrEnum):
    LARGEST = "largest"
    SMALLEST = "smallest"


class SizeRule(_Rule):
    """Final tie-break on size.

    ``cap_gb`` demotes over-cap releases behind every under-cap one; among
    over-cap releases the smallest wins regardless of ``direction``, since those
    are the ones closest to the cap.  Unlike :class:`SizeCapGbRule` this never
    eliminates a candidate.
    """

    type: Literal[RuleType.SIZE] = RuleType.SIZE
    direction: SizeDirection = SizeDirection.LARGEST
    cap_gb: float | None = Field(default=None, gt=0)


QualityRule = Annotated[
    ExcludePrereleaseRule
    | KeywordExcludeRule
    | SizeCapGbRule
    | RepackProperPriorityRule
    | ResolutionOrderRule
    | SourceOrderRule
    | HdrMatchRule
    | AudioMatchRule
    | SizeRule,
    Field(discriminator="type"),
]

FILTER_RULE_TYPES = (ExcludePrereleaseRule, KeywordExcludeRule, SizeCapGbRule)
PREFERENCE_RULE_TYPES = (
    RepackProperPriorityRule,
    ResolutionOrderRule,
    SourceOrderRule,
    HdrMatchRule,
    AudioMatchRule,
    SizeRule,
)


# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


class TieBreak(StrEnum):
    """How to order releases *within* one choice.

    ``BIGGEST`` is the practical stand-in for "highest bitrate": bitrate is not
    in a release name, and for two copies of the same film at the same
    resolution the larger file is the less compressed one.
    """

    BIGGEST = "biggest"
    SMALLEST = "smallest"
    CLOSEST_TO_GB = "closest_to_gb"
    NEWEST = "newest"
    MOST_SEEDERS = "most_seeders"


class ChoiceMatch(BaseModel):
    """What makes a release *this kind* of release.

    Every field is optional and an empty one is **no constraint**, so the
    all-empty match means "anything".  A non-empty list means the release's
    value has to be one of the listed ones; for the multi-valued tags (HDR,
    audio) it means the release carries at least one of them.

    A release whose size the indexer did not report never satisfies a size
    bound.  An unknown size is not evidence that a release is under 15 GB, and
    a choice is a claim about the release, not a default — so it falls through
    to a later choice rather than being let in on a guess.  (This is the
    opposite of :class:`SizeCapGbRule`, which keeps unknown sizes: there the
    consequence of guessing wrong is elimination, here it is only a lower
    ranking.)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolutions: list[Resolution] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    hdr: list[str] = Field(default_factory=list)
    audio: list[AudioTag] = Field(default_factory=list)
    min_size_gb: float | None = Field(default=None, gt=0)
    max_size_gb: float | None = Field(default=None, gt=0)

    @field_validator("hdr")
    @classmethod
    def _check_tokens(cls, values: list[str]) -> list[str]:
        bad = [v for v in values if not re.match(_HDR_TOKEN, v)]
        if bad:
            raise ValueError(f"unknown HDR tokens: {bad}")
        return values

    @property
    def is_anything(self) -> bool:
        """True when this match constrains nothing at all."""
        return not (
            self.resolutions
            or self.sources
            or self.hdr
            or self.audio
            or self.min_size_gb is not None
            or self.max_size_gb is not None
        )


class Choice(BaseModel):
    """One rung of the preference ladder: a kind of release, and how to pick
    between several of that kind.

    ``tie_break`` is optional.  Left unset, releases inside this choice are
    ordered by the profile's preference rules, which is usually what an admin
    wants.  Set, it decides first and the preference rules break *its* ties —
    that is how "the biggest one" becomes the whole answer for one rung while
    another rung stays "the biggest one under 15 GB".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    match: ChoiceMatch = Field(default_factory=ChoiceMatch)
    tie_break: TieBreak | None = None

    #: Target size for :attr:`TieBreak.CLOSEST_TO_GB`; ignored by every other
    #: tie-break.  Kept alongside rather than inside the enum so the form can
    #: hold onto a typed number while the admin switches between tie-breaks.
    tie_break_gb: float | None = Field(default=None, gt=0)


class QualityProfile(BaseModel):
    """A named profile: filters, an ordered list of choices, and tie-breakers.

    ``id`` is populated when the profile comes from the database and is ``None``
    for hand-built profiles in tests.

    ``choices`` defaults to empty, and empty means "one undifferentiated pool"
    — which is exactly how every profile behaved before choices existed, so a
    stored profile that predates them needs no migration.
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    name: str = ""
    rules: list[QualityRule] = Field(default_factory=list)
    choices: list[Choice] = Field(default_factory=list)

    @property
    def filters(self) -> list[QualityRule]:
        return [r for r in self.rules if isinstance(r, FILTER_RULE_TYPES)]

    @property
    def preferences(self) -> list[QualityRule]:
        """Preference rules in the order they should be applied — profile order."""
        return [r for r in self.rules if isinstance(r, PREFERENCE_RULE_TYPES)]


def default_profile(name: str = "Default") -> QualityProfile:
    """The conventional preference ordering, as a starting point for an admin.

    Matches the priority order described in the spec: repack/proper, then
    resolution, source, HDR, audio, and size as the final tie-break.

    **No filter rules at all**, which is what makes it safe to seed for a new
    admin who has not decided anything yet: it never eliminates a candidate, it
    only decides which of them is best.  The resolution ladder names every
    resolution rather than stopping at 1080p, so a profile built from this
    ranks an SD copy over an unparseable one instead of leaving both unranked.
    """
    return QualityProfile(
        name=name,
        rules=[
            RepackProperPriorityRule(),
            ResolutionOrderRule(
                values=[
                    Resolution.UHD_2160P,
                    Resolution.FHD_1080P,
                    Resolution.HD_720P,
                    Resolution.SD_480P,
                ]
            ),
            SourceOrderRule(
                values=[Source.WEB_DL, Source.WEBRIP, Source.BLURAY, Source.REMUX]
            ),
            HdrMatchRule(values=["DV_P8", "DV_P7", "HDR10+", "HDR10", "SDR"]),
            AudioMatchRule(values=[AudioTag.ATMOS, AudioTag.DTSX, AudioTag.TRUEHD]),
            SizeRule(direction=SizeDirection.LARGEST),
        ],
    )
