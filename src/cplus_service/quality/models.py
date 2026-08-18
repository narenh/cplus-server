"""Quality profile rule schema.

A quality profile is an **ordered list of rules**.  Two kinds of rule coexist in
that one list:

*Filter rules* eliminate a candidate from recommendation-eligibility entirely,
before any ranking happens.  Their position in the list is irrelevant — a filter
anywhere in the profile applies to everything.

*Preference rules* rank whatever survives the filters.  Their position **is**
load-bearing: the first preference rule in the list decides, the second breaks
its ties, and so on.

Two rules involve a GB number and they are deliberately distinct concepts:

``size_cap_gb`` (filter)
    Drops anything larger than the cap outright.  A release over the cap can
    never be recommended.

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


class QualityProfile(BaseModel):
    """A named, ordered list of rules.

    ``id`` is populated when the profile comes from the database and is ``None``
    for hand-built profiles in tests.
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    name: str = ""
    rules: list[QualityRule] = Field(default_factory=list)

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
    """
    return QualityProfile(
        name=name,
        rules=[
            RepackProperPriorityRule(),
            ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
            SourceOrderRule(
                values=[Source.WEB_DL, Source.WEBRIP, Source.BLURAY, Source.REMUX]
            ),
            HdrMatchRule(values=["DV_P8", "DV_P7", "HDR10+", "HDR10", "SDR"]),
            AudioMatchRule(values=[AudioTag.ATMOS, AudioTag.DTSX, AudioTag.TRUEHD]),
            SizeRule(direction=SizeDirection.LARGEST),
        ],
    )
