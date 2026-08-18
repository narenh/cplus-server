"""Quality profiles and the recommendation rule engine."""

from .engine import apply_filters, preferred_indexer_candidates, rank, recommend
from .models import (
    AudioMatchRule,
    ExcludePrereleaseRule,
    HdrMatchRule,
    KeywordExcludeRule,
    QualityProfile,
    QualityRule,
    RepackProperPriorityRule,
    ResolutionOrderRule,
    RuleType,
    SizeCapGbRule,
    SizeDirection,
    SizeRule,
    SourceOrderRule,
    default_profile,
)

__all__ = [
    "AudioMatchRule",
    "ExcludePrereleaseRule",
    "HdrMatchRule",
    "KeywordExcludeRule",
    "QualityProfile",
    "QualityRule",
    "RepackProperPriorityRule",
    "ResolutionOrderRule",
    "RuleType",
    "SizeCapGbRule",
    "SizeDirection",
    "SizeRule",
    "SourceOrderRule",
    "apply_filters",
    "default_profile",
    "preferred_indexer_candidates",
    "rank",
    "recommend",
]
