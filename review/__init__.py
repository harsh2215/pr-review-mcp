"""review package – public surface."""

from review.models import (
    # PR context models
    PRContext,
    BranchRef,
    ChangedFile,
    CommitInfo,
    RepoInfo,
    # Review result enums
    FindingCategory,
    FindingSeverity,
    FindingAction,
    # Review result models
    ReviewFinding,
    ReviewSummary,
    ReviewResult,
)
from review.normalizer import build_pr_context
from review.prompts import REVIEW_RUBRIC, RUBRIC_VERSION, get_review_prompt
from review.submission import (
    build_review_payload,
    classify_findings,
    parse_changed_lines,
    ClassifiedFinding,
    ReviewPayload,
)

__all__ = [
    # PR context
    "PRContext",
    "BranchRef",
    "ChangedFile",
    "CommitInfo",
    "RepoInfo",
    "build_pr_context",
    # Enums
    "FindingCategory",
    "FindingSeverity",
    "FindingAction",
    # Review result
    "ReviewFinding",
    "ReviewSummary",
    "ReviewResult",
    # Prompts
    "REVIEW_RUBRIC",
    "RUBRIC_VERSION",
    "get_review_prompt",
    # Submission
    "build_review_payload",
    "classify_findings",
    "parse_changed_lines",
    "ClassifiedFinding",
    "ReviewPayload",
]
