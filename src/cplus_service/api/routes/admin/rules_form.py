"""Turning the rule-builder form into stage 1's rule schema, and back.

The builder is stateless: no draft is held server-side between edits. Every
add/remove/move posts the whole current form, the server mutates the decoded
list and re-renders the rows fragment. That means a half-built profile cannot
outlive the page, two browser tabs cannot corrupt each other's draft, and there
is nothing to clean up.

Form encoding is index-prefixed, ``rules-<i>-<field>``. The indices only carry
order; they are renumbered from zero on every render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....quality.models import RuleType

#: Ordered-list rules take a comma-separated text input rather than a
#: multi-select: order is meaningful in these rules and a multi-select cannot
#: express it, since browsers submit options in document order.
LIST_RULES = {
    RuleType.RESOLUTION_ORDER,
    RuleType.SOURCE_ORDER,
    RuleType.HDR_MATCH,
    RuleType.AUDIO_MATCH,
}


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
        "Exclude pre-releases",
        "filter",
        "Drops CAM, HDCAM, TS/HDTS, telecine, HDRip, screener, DCP/DCPRip releases.",
    ),
    RuleSpec(
        RuleType.KEYWORD_EXCLUDE,
        "Exclude keywords",
        "filter",
        "Comma-separated. Drops any release whose title contains one of these.",
    ),
    RuleSpec(
        RuleType.SIZE_CAP_GB,
        "Maximum size (GB)",
        "filter",
        "Drops anything larger. Releases with no reported size are kept.",
    ),
    RuleSpec(
        RuleType.REPACK_PROPER_PRIORITY,
        "Prefer REPACK / PROPER",
        "preference",
        "Prefers a repack over the base release of the same title.",
    ),
    RuleSpec(
        RuleType.RESOLUTION_ORDER,
        "Resolution order",
        "preference",
        "Best first. Unlisted resolutions rank last but are not dropped.",
        ("2160p", "1080p", "720p", "480p", "unknown"),
    ),
    RuleSpec(
        RuleType.SOURCE_ORDER,
        "Source order",
        "preference",
        "Best first. Unlisted sources rank last but are not dropped.",
        ("WEB-DL", "WEBRip", "BluRay", "REMUX", "encode", "unknown"),
    ),
    RuleSpec(
        RuleType.HDR_MATCH,
        "HDR / Dolby Vision order",
        "preference",
        "Best first. DV matches any Dolby Vision profile; DV_P8 matches only that one.",
        ("DV_P7", "DV_P8", "DV_P5", "DV", "HDR10+", "HDR10", "SDR"),
    ),
    RuleSpec(
        RuleType.AUDIO_MATCH,
        "Audio order",
        "preference",
        "Best first. A release may carry several; it scores on its best match.",
        ("Atmos", "DTS:X", "TrueHD"),
    ),
    RuleSpec(
        RuleType.SIZE,
        "Size tie-break",
        "preference",
        "Final tie-break. A cap here only demotes larger releases; it never drops them.",
    ),
)

SPECS_BY_TYPE = {spec.type: spec for spec in RULE_SPECS}


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _as_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def decode_rules(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode ``rules-<i>-<field>`` form entries into rule dicts, in row order.

    Shape only — validity is stage 1's job, via
    :class:`~cplus_service.quality.models.QualityProfile`.
    """
    indices: set[int] = set()
    for key in form:
        if key.startswith("rules-") and key.endswith("-type"):
            try:
                indices.add(int(key.split("-")[1]))
            except (IndexError, ValueError):
                continue

    rules: list[dict[str, Any]] = []
    for index in sorted(indices):
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

    return rules


def encode_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stored rule into what one form row needs to render."""
    row = dict(rule)
    values = rule.get("values")
    if isinstance(values, list):
        row["values_text"] = ", ".join(str(value) for value in values)
    return row


def apply_op(
    rules: list[dict[str, Any]], op: str, index: int, rule_type: str | None
) -> list[dict[str, Any]]:
    """Apply one builder operation to the decoded rule list.

    Out-of-range indices are ignored rather than raising: the form is client
    input, and a stale row number should redraw the list, not 500.
    """
    working = list(rules)

    if op == "add" and rule_type:
        working.append({"type": rule_type, "enabled": True})
    elif op == "remove" and 0 <= index < len(working):
        working.pop(index)
    elif op == "up" and 0 < index < len(working):
        working[index - 1], working[index] = working[index], working[index - 1]
    elif op == "down" and 0 <= index < len(working) - 1:
        working[index + 1], working[index] = working[index], working[index + 1]

    return working
