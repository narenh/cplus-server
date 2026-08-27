"""Turning the profile builder's form into stage 1's schema, and back.

The builder is stateless: no draft is held server-side between edits. Every
add/remove/move posts the whole current form, the server mutates the decoded
lists and re-renders the builder. That means a half-built profile cannot
outlive the page, two browser tabs cannot corrupt each other's draft, and there
is nothing to clean up.

Two encodings, one per section of the page:

``rules-<i>-<field>``
    Filter and preference rules, in one list. Position carries meaning for
    preferences only, so the list is kept canonical — filters first, then
    preferences — and the move buttons swap within a kind. That is what lets a
    single flat encoding drive two visually separate sections without the
    filters' (irrelevant) positions leaking into the preferences' (meaningful)
    ones.

``choices-<i>-<field>``
    The ordered choices. Every choice row carries a hidden ``present`` field:
    a choice whose checkboxes are all clear submits nothing else at all, and
    without a marker the server would decode it as having been deleted.

Indices only carry order; they are renumbered from zero on every render.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ....quality.models import RuleType, TieBreak

#: Ordered-list rules take a comma-separated text input rather than a
#: multi-select: order is meaningful in these rules and a multi-select cannot
#: express it, since browsers submit options in document order.
LIST_RULES = {
    RuleType.RESOLUTION_ORDER,
    RuleType.SOURCE_ORDER,
    RuleType.HDR_MATCH,
    RuleType.AUDIO_MATCH,
}

FILTER_KIND = "filter"
PREFERENCE_KIND = "preference"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """What the UI needs to know to render one rule type."""

    type: RuleType
    label: str
    kind: str  # "filter" | "preference"
    help: str
    choices: tuple[str, ...] = ()


RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec(
        RuleType.EXCLUDE_PRERELEASE,
        "Pre-releases",
        FILTER_KIND,
        "CAM, HDCAM, TS/HDTS, telecine, HDRip, screener, DCP/DCPRip.",
    ),
    RuleSpec(
        RuleType.KEYWORD_EXCLUDE,
        "Titles containing…",
        FILTER_KIND,
        "Comma-separated. Matched anywhere in the release title, ignoring case.",
    ),
    RuleSpec(
        RuleType.SIZE_CAP_GB,
        "Anything over… (GB)",
        FILTER_KIND,
        "A release with no reported size is kept — an unknown size is not evidence.",
    ),
    RuleSpec(
        RuleType.REPACK_PROPER_PRIORITY,
        "REPACK / PROPER first",
        PREFERENCE_KIND,
        "Only against the release it replaces, matched on the title itself.",
    ),
    RuleSpec(
        RuleType.RESOLUTION_ORDER,
        "Resolution order",
        PREFERENCE_KIND,
        "Best first. Anything unlisted ranks last — it is not dropped.",
        ("2160p", "1080p", "720p", "480p", "unknown"),
    ),
    RuleSpec(
        RuleType.SOURCE_ORDER,
        "Source order",
        PREFERENCE_KIND,
        "Best first. Anything unlisted ranks last — it is not dropped.",
        ("WEB-DL", "WEBRip", "BluRay", "REMUX", "encode", "unknown"),
    ),
    RuleSpec(
        RuleType.HDR_MATCH,
        "Dynamic range order",
        PREFERENCE_KIND,
        "Best first. DV matches any Dolby Vision profile; DV_P8 matches only that one.",
        ("DV_P7", "DV_P8", "DV_P5", "DV", "HDR10+", "HDR10", "SDR"),
    ),
    RuleSpec(
        RuleType.AUDIO_MATCH,
        "Audio order",
        PREFERENCE_KIND,
        "Best first. A release may carry several; it scores on its best match.",
        ("Atmos", "DTS:X", "TrueHD"),
    ),
    RuleSpec(
        RuleType.SIZE,
        "File size",
        PREFERENCE_KIND,
        "A cap here only demotes larger releases; to drop them, filter instead.",
    ),
)

SPECS_BY_TYPE = {spec.type: spec for spec in RULE_SPECS}
FILTER_TYPES = {spec.type for spec in RULE_SPECS if spec.kind == FILTER_KIND}


# --------------------------------------------------------------------------- #
# Choice vocabularies
#
# Unordered, unlike the preference rules': a choice asks "is it one of these?",
# so these render as checkboxes and the order they are ticked in means nothing.
# --------------------------------------------------------------------------- #

RESOLUTION_OPTIONS = ("2160p", "1080p", "720p", "480p", "unknown")
SOURCE_OPTIONS = ("WEB-DL", "WEBRip", "BluRay", "REMUX", "encode", "unknown")
HDR_OPTIONS = ("DV", "DV_P7", "DV_P8", "DV_P5", "HDR10+", "HDR10", "SDR")
AUDIO_OPTIONS = ("Atmos", "DTS:X", "TrueHD")

#: ``value, label`` — the empty value defers to the profile's tie-breakers,
#: which is the default and reads as such in the dropdown.
TIE_BREAK_OPTIONS: tuple[tuple[str, str], ...] = (
    ("", "the tie-breakers below"),
    (TieBreak.BIGGEST.value, "the biggest file"),
    (TieBreak.SMALLEST.value, "the smallest file"),
    (TieBreak.CLOSEST_TO_GB.value, "the closest to a size"),
    (TieBreak.NEWEST.value, "the newest"),
    (TieBreak.MOST_SEEDERS.value, "the most seeders"),
)

CHOICE_FIELDS = (
    ("resolutions", "Resolution", RESOLUTION_OPTIONS),
    ("sources", "Source", SOURCE_OPTIONS),
    ("hdr", "Dynamic range", HDR_OPTIONS),
    ("audio", "Audio", AUDIO_OPTIONS),
)


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _as_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _many(form: Mapping[str, Any], key: str) -> list[str]:
    """Every value submitted under ``key``.

    Starlette hands multi-valued fields (a checkbox group) back through
    ``getlist``; a plain dict, which is what most tests build, has one value
    per key. Supporting both keeps the test-facing shape simple.
    """
    getlist = getattr(form, "getlist", None)
    if getlist is not None:
        return [str(value).strip() for value in getlist(key) if str(value).strip()]
    raw = form.get(key)
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        return [str(value).strip() for value in raw if str(value).strip()]
    return _split(str(raw))


def _indices(form: Mapping[str, Any], prefix: str, marker: str) -> list[int]:
    found: set[int] = set()
    for key in form:
        if key.startswith(f"{prefix}-") and key.endswith(f"-{marker}"):
            try:
                found.add(int(key.split("-")[1]))
            except (IndexError, ValueError):
                continue
    return sorted(found)


def decode_rules(form: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode ``rules-<i>-<field>`` entries into rule dicts, canonically ordered.

    Shape only — validity is stage 1's job, via
    :class:`~cplus_service.quality.models.QualityProfile`.
    """
    rules: list[dict[str, Any]] = []
    for index in _indices(form, "rules", "type"):

        def field(name: str, _i: int = index) -> str:
            return str(form.get(f"rules-{_i}-{name}", "") or "").strip()

        rule_type = field("type")
        if not rule_type:
            continue

        rule: dict[str, Any] = {"type": rule_type}
        if rule_type in (RuleType.EXCLUDE_PRERELEASE, RuleType.REPACK_PROPER_PRIORITY):
            # An unchecked checkbox submits nothing at all.
            rule["enabled"] = field("enabled") != ""
        elif rule_type == RuleType.KEYWORD_EXCLUDE:
            rule["values"] = _split(field("values"))
        elif rule_type == RuleType.SIZE_CAP_GB:
            rule["value"] = _as_float(field("value"))
        elif rule_type == RuleType.SIZE:
            rule["direction"] = field("direction") or "largest"
            rule["cap_gb"] = _as_float(field("cap_gb"))
        elif rule_type in LIST_RULES:
            rule["values"] = _split(field("values"))
        rules.append(rule)

    return canonical(rules)


def decode_choices(form: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode ``choices-<i>-<field>`` entries into choice dicts, in row order."""
    choices: list[dict[str, Any]] = []
    for index in _indices(form, "choices", "present"):

        def field(name: str, _i: int = index) -> str:
            return str(form.get(f"choices-{_i}-{name}", "") or "").strip()

        tie_break = field("tie_break")
        choices.append(
            {
                "match": {
                    "resolutions": _many(form, f"choices-{index}-resolutions"),
                    "sources": _many(form, f"choices-{index}-sources"),
                    "hdr": _many(form, f"choices-{index}-hdr"),
                    "audio": _many(form, f"choices-{index}-audio"),
                    "min_size_gb": _as_float(field("min_size_gb")),
                    "max_size_gb": _as_float(field("max_size_gb")),
                },
                "tie_break": tie_break or None,
                "tie_break_gb": _as_float(field("tie_break_gb")),
            }
        )
    return choices


# --------------------------------------------------------------------------- #
# Encoding, back out to the form
# --------------------------------------------------------------------------- #


def kind_of(rule: Mapping[str, Any]) -> str:
    return FILTER_KIND if rule.get("type") in FILTER_TYPES else PREFERENCE_KIND


def canonical(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filters first, then preferences, each keeping its relative order.

    Filter position means nothing to the engine, so pinning them to the front
    costs no behaviour and buys one thing the page needs: the preferences are
    then contiguous, and "move up" among them is a swap with the neighbouring
    index rather than a search for the previous rule of the same kind.
    """
    return [rule for rule in rules if kind_of(rule) == FILTER_KIND] + [
        rule for rule in rules if kind_of(rule) != FILTER_KIND
    ]


def encode_rule(rule: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    """Flatten a stored rule into what one form row needs to render."""
    row = dict(rule)
    values = rule.get("values")
    if isinstance(values, list):
        row["values_text"] = ", ".join(str(value) for value in values)
    row["index"] = index
    row["kind"] = kind_of(rule)
    return row


def encode_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every rule as a form row, numbered in canonical order."""
    return [encode_rule(rule, index) for index, rule in enumerate(canonical(rules))]


def encode_choice(choice: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    """Flatten a stored choice into what one form row needs to render.

    The multi-valued fields become sets so the template can ask
    ``"2160p" in row.resolutions`` when deciding which boxes are ticked.
    """
    match = choice.get("match") or {}
    return {
        "index": index,
        "resolutions": set(match.get("resolutions") or ()),
        "sources": set(match.get("sources") or ()),
        "hdr": set(match.get("hdr") or ()),
        "audio": set(match.get("audio") or ()),
        "min_size_gb": match.get("min_size_gb"),
        "max_size_gb": match.get("max_size_gb"),
        "tie_break": choice.get("tie_break") or "",
        "tie_break_gb": choice.get("tie_break_gb"),
    }


def encode_choices(choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [encode_choice(choice, index) for index, choice in enumerate(choices)]


# --------------------------------------------------------------------------- #
# Builder operations
# --------------------------------------------------------------------------- #

def empty_choice() -> dict[str, Any]:
    """A fresh, unconstrained choice: matches anything, defers on ties.

    Built per call rather than copied from a constant — a shared nested dict
    would let two rows in one list end up as the same object.
    """
    return {
        "match": {
            "resolutions": [],
            "sources": [],
            "hdr": [],
            "audio": [],
            "min_size_gb": None,
            "max_size_gb": None,
        },
        "tie_break": None,
        "tie_break_gb": None,
    }


def apply_rule_op(
    rules: list[dict[str, Any]], op: str, index: int, rule_type: str | None
) -> list[dict[str, Any]]:
    """Apply one builder operation to the decoded rule list.

    Out-of-range indices are ignored rather than raising: the form is client
    input, and a stale row number should redraw the list, not 500.

    A move never crosses the filter/preference boundary. Refusing it is right
    rather than merely safe: the two sections are different questions, and a
    filter that slid into the preference list would change nothing about what
    the profile does while looking as though it had.
    """
    working = canonical(rules)

    if op == "add" and rule_type:
        working.append({"type": rule_type, "enabled": True})
        return canonical(working)
    if op == "remove" and 0 <= index < len(working):
        working.pop(index)
    elif op in ("up", "down") and 0 <= index < len(working):
        other = index - 1 if op == "up" else index + 1
        if 0 <= other < len(working) and kind_of(working[other]) == kind_of(working[index]):
            working[other], working[index] = working[index], working[other]

    return working


def apply_choice_op(
    choices: list[dict[str, Any]], op: str, index: int
) -> list[dict[str, Any]]:
    """Apply one builder operation to the decoded choice list.

    Order is the whole meaning of this list, so unlike the rules there is no
    canonical form to restore — the admin's order *is* the answer.
    """
    working = list(choices)

    if op == "add":
        working.append(empty_choice())
    elif op == "remove" and 0 <= index < len(working):
        working.pop(index)
    elif op == "up" and 0 < index < len(working):
        working[index - 1], working[index] = working[index], working[index - 1]
    elif op == "down" and 0 <= index < len(working) - 1:
        working[index + 1], working[index] = working[index], working[index + 1]

    return working
