"""A profile, in words.

The admin UI can show a profile's rules as form controls, but a list of
controls does not say what the profile *does* — which is the complaint this
module exists to answer. Every builder page renders a summary from here, so
what an admin reads back is generated from the same objects the engine
consumes rather than from a description someone maintained by hand.

Pure and dependency-free, like the engine: text in, no I/O. Phrases are written
to read as a sentence fragment ("anything over 60 GB", "biggest file"), so the
template can compose them without string surgery.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..release.models import Resolution, Source
from .models import (
    AudioMatchRule,
    Choice,
    ChoiceMatch,
    ExcludePrereleaseRule,
    HdrMatchRule,
    KeywordExcludeRule,
    QualityProfile,
    QualityRule,
    RepackProperPriorityRule,
    ResolutionOrderRule,
    SizeCapGbRule,
    SizeDirection,
    SizeRule,
    SourceOrderRule,
    TieBreak,
)

#: The words for a resolution, where the bare token is not what an admin says.
_RESOLUTION_WORDS = {
    Resolution.UHD_2160P: "4K",
    Resolution.UNKNOWN: "no resolution in the name",
}
_SOURCE_WORDS = {Source.UNKNOWN: "no source in the name"}

ANYTHING = "anything"


def gb(value: float) -> str:
    """A GB number as an admin would write it: ``15 GB``, not ``15.0 GB``."""
    return f"{value:g} GB"


def _join(parts: list[str], conjunction: str = "or") -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def describe_filter(rule: QualityRule) -> str:
    """What this filter refuses, e.g. ``anything over 60 GB``.

    Reads as the object of "never grab …".
    """
    if isinstance(rule, ExcludePrereleaseRule):
        if not rule.enabled:
            return "nothing — pre-releases are allowed"
        return "CAM, telesync, telecine and screener releases"
    if isinstance(rule, KeywordExcludeRule):
        words = [value.strip() for value in rule.values if value.strip()]
        if not words:
            return "nothing — no keywords listed"
        return f"titles containing {_join([repr(word) for word in words])}"
    if isinstance(rule, SizeCapGbRule):
        return f"anything over {gb(rule.value)}"
    return ""


def describe_preference(rule: QualityRule) -> str:
    """What this preference prefers, as the object of "prefer …"."""
    if isinstance(rule, RepackProperPriorityRule):
        if not rule.enabled:
            return "no REPACK preference"
        return "a REPACK or PROPER over the release it replaces"
    if isinstance(rule, ResolutionOrderRule):
        return _ordered("resolution", [value.value for value in rule.values])
    if isinstance(rule, SourceOrderRule):
        return _ordered("source", list(rule.values))
    if isinstance(rule, HdrMatchRule):
        return _ordered("dynamic range", list(rule.values))
    if isinstance(rule, AudioMatchRule):
        return _ordered("audio", [value.value for value in rule.values])
    if isinstance(rule, SizeRule):
        direction = "the biggest" if rule.direction is SizeDirection.LARGEST else "the smallest"
        if rule.cap_gb is not None:
            return f"{direction} file, with anything over {gb(rule.cap_gb)} demoted"
        return f"{direction} file"
    return ""


def _ordered(label: str, values: list[str]) -> str:
    if not values:
        return f"{label} — nothing listed, so this rule decides nothing"
    return f"{label}: {' → '.join(values)}"


# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


def describe_match(match: ChoiceMatch) -> str:
    """The kind of release a choice is about, e.g. ``4K WEB-DL under 15 GB``."""
    if match.is_anything:
        return ANYTHING

    parts: list[str] = []
    if match.resolutions:
        parts.append(
            _join([_RESOLUTION_WORDS.get(value, value.value) for value in match.resolutions])
        )
    if match.sources:
        parts.append(_join([_SOURCE_WORDS.get(value, value.value) for value in match.sources]))
    if match.hdr:
        parts.append(_join(list(match.hdr)))
    if match.audio:
        parts.append(_join([value.value for value in match.audio]))

    if match.min_size_gb is not None and match.max_size_gb is not None:
        parts.append(f"between {gb(match.min_size_gb)} and {gb(match.max_size_gb)}")
    elif match.max_size_gb is not None:
        parts.append(f"under {gb(match.max_size_gb)}")
    elif match.min_size_gb is not None:
        parts.append(f"over {gb(match.min_size_gb)}")

    return " · ".join(parts)


def describe_tie_break(choice: Choice) -> str:
    """How a choice picks between its own matches, or ``""`` when it defers.

    An empty string is not "no ordering": it means the profile's tie-breakers
    decide, which is the default and the common case.
    """
    if choice.tie_break is None:
        return ""
    if choice.tie_break is TieBreak.BIGGEST:
        return "biggest file"
    if choice.tie_break is TieBreak.SMALLEST:
        return "smallest file"
    if choice.tie_break is TieBreak.NEWEST:
        return "newest"
    if choice.tie_break is TieBreak.MOST_SEEDERS:
        return "most seeders"
    if choice.tie_break_gb is None:
        # The size was never filled in; say what will happen rather than
        # printing a target that does not exist.
        return "biggest file"
    return f"closest to {gb(choice.tie_break_gb)}"


def describe_choice(choice: Choice) -> str:
    """One rung, whole: ``4K WEB-DL, biggest file``."""
    tie_break = describe_tie_break(choice)
    if tie_break:
        return f"{describe_match(choice.match)}, {tie_break}"
    return describe_match(choice.match)


def ordinal(index: int) -> str:
    """``0 -> 1st``. Choices are numbered for the admin, not indexed."""
    number = index + 1
    if 11 <= number % 100 <= 13:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"


# --------------------------------------------------------------------------- #
# The whole profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """A profile read back as three short lists, in the order they apply."""

    never_grab: list[str] = field(default_factory=list)
    choices: list[str] = field(default_factory=list)
    tie_breaks: list[str] = field(default_factory=list)

    #: True when the profile would take literally anything, in indexer order —
    #: worth saying out loud, since it looks like a profile but decides nothing.
    @property
    def is_empty(self) -> bool:
        return not (self.never_grab or self.choices or self.tie_breaks)

    @property
    def catch_all_choice(self) -> int | None:
        """Index of a choice that matches everything and is not the last.

        Choices after it can never apply — a real mistake, and an invisible one
        without something to point at it.
        """
        for index, text in enumerate(self.choices[:-1]):
            if text == ANYTHING or text.startswith(f"{ANYTHING},"):
                return index
        return None


def summarise(profile: QualityProfile) -> ProfileSummary:
    """The profile in plain English, ready to render."""
    return ProfileSummary(
        never_grab=[describe_filter(rule) for rule in profile.filters],
        choices=[describe_choice(choice) for choice in profile.choices],
        tie_breaks=[describe_preference(rule) for rule in profile.preferences],
    )
