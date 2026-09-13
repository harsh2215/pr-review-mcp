"""
review/submission.py
--------------------
Validation and building logic for submitting a PR review to GitHub.

Responsibilities:
- Parse unified diff patches to build a set of commentable lines.
- Validate every INLINE ReviewFinding against the current PR diff.
- Classify findings into INLINE / SUMMARY / DISCARD.
- Build the GitHub review payload (body markdown + inline comment list).
- Return a structured DryRunResult preview (read-only).

Security invariants:
- This module never touches the GitHub token.
- DryRunResult never includes auth headers or credentials.

Architecture note:
- All read (GET) operations happen here via GitHubClient.
- The actual POST is performed in server.py only when dry_run=False.
- This separation keeps the submission logic independently testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from review.models import (
    FindingAction,
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewSummary,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum confidence to keep a finding as INLINE.
_INLINE_MIN_CONFIDENCE: float = 0.70
# Minimum confidence to keep a finding as SUMMARY.
_SUMMARY_MIN_CONFIDENCE: float = 0.50

# GitHub line-comment API uses side="RIGHT" for the new version of a file.
_COMMENT_SIDE = "RIGHT"

# Unified diff hunk header pattern, e.g.  @@ -5,7 +10,8 @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ---------------------------------------------------------------------------
# Review event enum
# ---------------------------------------------------------------------------


class ReviewEvent(str):
    """Allowed values for the GitHub Pull Request Review event field.

    COMMENT        – Submit review comments without explicitly approving or
                     requesting changes.  Safe default.
    REQUEST_CHANGES – Indicate that changes are required before the PR can
                     be merged.

    APPROVE is intentionally excluded from this phase.
    """

    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"

    @classmethod
    def validate(cls, value: str) -> "ReviewEvent":
        """Return the canonical value or raise ValueError for unknown events."""
        normalised = value.strip().upper()
        allowed = {cls.COMMENT, cls.REQUEST_CHANGES}
        if normalised not in allowed:
            raise ValueError(
                f"Invalid review event {value!r}. "
                f"Allowed values: {sorted(allowed)}. "
                "Note: APPROVE is not supported in this version."
            )
        return normalised  # type: ignore[return-value]



# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def parse_changed_lines(patch: str) -> frozenset[int]:
    """Return the set of *added/modified* line numbers visible in a diff patch.

    Only lines prefixed with ``+`` (added) contribute to the set.
    Lines prefixed with ``-`` (removed) and context lines (space prefix) do NOT.

    This is the source of truth used to validate INLINE findings: GitHub only
    allows comments on lines that appear in the diff.

    Args:
        patch: Raw unified diff patch string from the GitHub ``files`` endpoint.
               May be empty or None-equivalent.

    Returns:
        Frozenset of 1-based line numbers (in the *new* file) that are
        commentable.  Returns an empty frozenset for binary/empty patches.
    """
    if not patch:
        return frozenset()

    commentable: set[int] = set()
    current_new_line: int = 0

    for raw_line in patch.splitlines():
        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            # New-file start line from the hunk header.
            current_new_line = int(hunk_match.group(1))
            continue

        if raw_line.startswith("+"):
            commentable.add(current_new_line)
            current_new_line += 1
        elif raw_line.startswith("-"):
            # Removed line – does not advance new-file line counter.
            pass
        else:
            # Context line (space prefix) – advances counter but is not commentable.
            current_new_line += 1

    return frozenset(commentable)


# ---------------------------------------------------------------------------
# Dataclasses for classified findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedFinding:
    """A ReviewFinding after validation with its final action and discard reason."""

    finding: ReviewFinding
    action: FindingAction
    discard_reason: str | None = None

    @property
    def finding_id(self) -> str:
        return self.finding.finding_id


@dataclass
class ReviewPayload:
    """The complete GitHub review payload ready to be submitted (or previewed)."""

    # Context
    pr_number: int
    head_sha: str

    # Review event (COMMENT or REQUEST_CHANGES)
    event: str = "COMMENT"

    # Classified findings
    inline: list[ClassifiedFinding] = field(default_factory=list)
    summary: list[ClassifiedFinding] = field(default_factory=list)
    discarded: list[ClassifiedFinding] = field(default_factory=list)

    # Rendered outputs
    review_body: str = ""
    inline_comments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict (safe for dry-run response)."""
        return {
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "event": self.event,
            "inline_findings": [
                {
                    "finding_id": cf.finding_id,
                    "category": cf.finding.category.value,
                    "severity": cf.finding.severity.value,
                    "confidence": cf.finding.confidence,
                    "file": cf.finding.file,
                    "line": cf.finding.line,
                    "title": cf.finding.title,
                    "action": cf.action.value,
                }
                for cf in self.inline
            ],
            "summary_findings": [
                {
                    "finding_id": cf.finding_id,
                    "category": cf.finding.category.value,
                    "severity": cf.finding.severity.value,
                    "confidence": cf.finding.confidence,
                    "title": cf.finding.title,
                    "action": cf.action.value,
                }
                for cf in self.summary
            ],
            "discarded_findings": [
                {
                    "finding_id": cf.finding_id,
                    "title": cf.finding.title,
                    "action": cf.action.value,
                    "discard_reason": cf.discard_reason,
                }
                for cf in self.discarded
            ],
            "review_body": self.review_body,
            "inline_comments": self.inline_comments,
            "counts": {
                "inline": len(self.inline),
                "summary": len(self.summary),
                "discarded": len(self.discarded),
            },
        }


# ---------------------------------------------------------------------------
# Core classification logic
# ---------------------------------------------------------------------------


def _build_commentable_index(pr_context_files: list[Any]) -> dict[str, frozenset[int]]:
    """Build a mapping from file path → set of commentable new-file line numbers.

    Args:
        pr_context_files: List of ``ChangedFile`` objects from a ``PRContext``.

    Returns:
        Dict mapping repo-relative file path to the frozenset of commentable
        line numbers in that file's diff.
    """
    index: dict[str, frozenset[int]] = {}
    for cf in pr_context_files:
        index[cf.path] = parse_changed_lines(cf.patch or "")
    return index


def classify_findings(
    result: ReviewResult,
    commentable_index: dict[str, frozenset[int]],
) -> tuple[list[ClassifiedFinding], list[ClassifiedFinding], list[ClassifiedFinding]]:
    """Classify each finding in a ReviewResult into INLINE / SUMMARY / DISCARD.

    Classification rules (applied in priority order):
    1. Confidence < 0.50 → DISCARD (speculative).
    2. action == DISCARD → DISCARD (Claude already chose to discard).
    3. INLINE requested:
       a. file missing → downgrade to SUMMARY (if confidence ≥ 0.50).
       b. file not in PR diff → downgrade to SUMMARY.
       c. line missing → downgrade to SUMMARY.
       d. line not in commentable set → downgrade to SUMMARY.
       e. all checks pass → INLINE.
    4. SUMMARY → SUMMARY (confidence ≥ 0.50).
    5. Everything else → DISCARD.

    One bad finding never invalidates the entire review.

    Args:
        result: The ReviewResult produced by Claude.
        commentable_index: Mapping of file → commentable line numbers from the
                           *current* PR diff (re-fetched, not from Claude).

    Returns:
        Three lists: (inline, summary, discarded) of ClassifiedFinding.
    """
    inline: list[ClassifiedFinding] = []
    summary_findings: list[ClassifiedFinding] = []
    discarded: list[ClassifiedFinding] = []

    for finding in result.findings:
        # Rule 1: too low confidence.
        if finding.confidence < _SUMMARY_MIN_CONFIDENCE:
            discarded.append(ClassifiedFinding(
                finding=finding,
                action=FindingAction.DISCARD,
                discard_reason=f"confidence {finding.confidence:.2f} < {_SUMMARY_MIN_CONFIDENCE} threshold",
            ))
            continue

        # Rule 2: Claude already chose to discard.
        if finding.action == FindingAction.DISCARD:
            discarded.append(ClassifiedFinding(
                finding=finding,
                action=FindingAction.DISCARD,
                discard_reason="Claude action=discard",
            ))
            continue

        # Rule 3: INLINE requested.
        if finding.action == FindingAction.INLINE:
            if finding.confidence < _INLINE_MIN_CONFIDENCE:
                # Not confident enough for inline – downgrade to summary.
                summary_findings.append(ClassifiedFinding(
                    finding=finding,
                    action=FindingAction.SUMMARY,
                    discard_reason=None,
                ))
                continue

            # Validate location.
            if not finding.file:
                summary_findings.append(ClassifiedFinding(
                    finding=finding,
                    action=FindingAction.SUMMARY,
                    discard_reason="inline requested but no file provided; downgraded to summary",
                ))
                continue

            if finding.file not in commentable_index:
                summary_findings.append(ClassifiedFinding(
                    finding=finding,
                    action=FindingAction.SUMMARY,
                    discard_reason=(
                        f"file '{finding.file}' not in PR diff; downgraded to summary"
                    ),
                ))
                continue

            if finding.line is None:
                summary_findings.append(ClassifiedFinding(
                    finding=finding,
                    action=FindingAction.SUMMARY,
                    discard_reason="inline requested but no line provided; downgraded to summary",
                ))
                continue

            commentable_lines = commentable_index[finding.file]
            if finding.line not in commentable_lines:
                summary_findings.append(ClassifiedFinding(
                    finding=finding,
                    action=FindingAction.SUMMARY,
                    discard_reason=(
                        f"line {finding.line} in '{finding.file}' is not a changed/commentable "
                        f"line in the PR diff; downgraded to summary"
                    ),
                ))
                continue

            # All checks pass – INLINE.
            inline.append(ClassifiedFinding(finding=finding, action=FindingAction.INLINE))
            continue

        # Rule 4: SUMMARY.
        if finding.action == FindingAction.SUMMARY:
            summary_findings.append(ClassifiedFinding(finding=finding, action=FindingAction.SUMMARY))
            continue

        # Rule 5: Catch-all discard.
        discarded.append(ClassifiedFinding(
            finding=finding,
            action=FindingAction.DISCARD,
            discard_reason=f"unrecognised action '{finding.action}'",
        ))

    return inline, summary_findings, discarded


# ---------------------------------------------------------------------------
# Review body renderer
# ---------------------------------------------------------------------------


def _severity_prefix(severity: FindingSeverity) -> str:
    """Return a short human-readable severity word — used only in prose, not as a badge."""
    return {
        FindingSeverity.CRITICAL: "Critical",
        FindingSeverity.HIGH: "High-priority",
        FindingSeverity.MEDIUM: "Medium-priority",
        FindingSeverity.LOW: "Minor",
    }.get(severity, "")


def _render_inline_comment_body(f: ReviewFinding) -> str:
    """Render a clean, human-readable inline diff comment for a single finding.

    Does NOT include finding_id, confidence, category label, or severity badges.
    """
    lines: list[str] = []
    lines.append(f"**{f.title}**")
    lines.append("")
    lines.append(f.description)
    if f.recommendation:
        lines.append("")
        lines.append(f"**Fix:** {f.recommendation}")
    return "\n".join(lines)


def build_review_body(
    result: ReviewResult,
    inline: list[ClassifiedFinding],
    summary: list[ClassifiedFinding],
    discarded: list[ClassifiedFinding],
) -> str:
    """Render a clean, professional GitHub review body markdown.

    Produces output that looks like a senior-engineer code review:
    - No finding IDs, confidence values, or internal category/severity labels.
    - Summary findings are written as natural prose sections.
    - Inline findings are NOT duplicated in the body.
    - No HTML entities or literal backslash-n sequences.

    Args:
        result: The full ReviewResult (for summary narrative).
        inline: INLINE classified findings.
        summary: SUMMARY classified findings.
        discarded: DISCARD classified findings (not rendered).

    Returns:
        A clean markdown string suitable for the GitHub review body.
    """
    parts: list[str] = []

    # --- Header + verdict ---
    parts.append("## AI Code Review")
    parts.append("")
    parts.append(result.summary.verdict)
    parts.append("")
    parts.append(result.summary.overview)

    # --- Strengths (only if genuinely useful) ---
    if result.summary.strengths:
        parts.append("")
        parts.append("### Strengths")
        for s in result.summary.strengths:
            parts.append(f"- {s}")

    # --- Summary findings (natural prose, no IDs / confidence) ---
    if summary:
        # Group critical/high separately from medium/low for readability.
        critical_high = [
            cf for cf in summary
            if cf.finding.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)
        ]
        medium_low = [
            cf for cf in summary
            if cf.finding.severity in (FindingSeverity.MEDIUM, FindingSeverity.LOW)
        ]

        if critical_high:
            parts.append("")
            parts.append("### Key concerns")
            parts.append("")
            for cf in critical_high:
                f = cf.finding
                parts.append(f"**{f.title}**")
                parts.append("")
                parts.append(f.description)
                if f.recommendation:
                    parts.append("")
                    parts.append(f"*Recommendation:* {f.recommendation}")
                parts.append("")

        if medium_low:
            parts.append("")
            parts.append("### Other concerns")
            parts.append("")
            for cf in medium_low:
                f = cf.finding
                parts.append(f"**{f.title}**")
                parts.append("")
                parts.append(f.description)
                if f.recommendation:
                    parts.append("")
                    parts.append(f"*Recommendation:* {f.recommendation}")
                parts.append("")

    # --- Key risks from summary (if not already shown via findings) ---
    if result.summary.risks and not summary:
        parts.append("")
        parts.append("### Key concerns")
        for r in result.summary.risks:
            parts.append(f"- {r}")

    # Strip any trailing blank lines then add a clean separator.
    while parts and parts[-1] == "":
        parts.pop()

    return "\n".join(parts)


def build_inline_comments(inline: list[ClassifiedFinding]) -> list[dict[str, Any]]:
    """Build the GitHub API inline comment payload from validated INLINE findings.

    Each comment is rendered as a natural code-review comment with no internal
    metadata (no finding_id, no confidence, no category/severity labels).

    Uses the line-based API (``line`` + ``side``) rather than the deprecated
    position-based API.

    Args:
        inline: Validated INLINE ClassifiedFindings (all locations confirmed in diff).

    Returns:
        List of dicts ready to embed in the GitHub review ``comments`` field.
    """
    comments: list[dict[str, Any]] = []
    for cf in inline:
        comments.append({
            "path": cf.finding.file,
            "line": cf.finding.line,
            "side": _COMMENT_SIDE,
            "body": _render_inline_comment_body(cf.finding),
        })
    return comments


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_review_payload(
    result: ReviewResult,
    pr_context_files: list[Any],
    head_sha: str,
    event: str = "COMMENT",
) -> ReviewPayload:
    """Validate findings against the current diff and build the full review payload.

    This is the primary entry point called by the MCP tool.  It:
    1. Builds a commentable-lines index from the current PR diff.
    2. Classifies every finding (INLINE / SUMMARY / DISCARD).
    3. Renders the review body markdown.
    4. Builds the inline comment API payload.

    Args:
        result: ReviewResult produced by Claude.
        pr_context_files: List of ``ChangedFile`` objects from a freshly
                          fetched ``PRContext`` (not the stale one from Claude).
        head_sha: The current PR head SHA (from the fresh PRContext).
        event: GitHub review event — "COMMENT" or "REQUEST_CHANGES". Must
               have been validated by ``ReviewEvent.validate()`` before calling
               this function.

    Returns:
        A fully populated ``ReviewPayload``.
    """
    commentable_index = _build_commentable_index(pr_context_files)
    inline, summary, discarded = classify_findings(result, commentable_index)

    review_body = build_review_body(result, inline, summary, discarded)
    inline_comments = build_inline_comments(inline)

    return ReviewPayload(
        pr_number=result.pr_number,
        head_sha=head_sha,
        event=event,
        inline=inline,
        summary=summary,
        discarded=discarded,
        review_body=review_body,
        inline_comments=inline_comments,
    )
