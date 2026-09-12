"""
review/models.py
----------------
Domain models for the PR Review MCP.

Two distinct model families:

1. **PR context models** (PRContext, RepoInfo, BranchRef, CommitInfo, ChangedFile)
   These represent normalised GitHub API data that Claude uses as input.

2. **Review result models** (ReviewFinding, ReviewResult)
   These represent Claude's structured output after analysing a PR.
   The MCP server does NOT populate these – they are the *contract* that tells
   Claude exactly what shape to produce.

Design principles:
- Typed enums for all categorical fields so invalid values are caught at
  validation time rather than silently accepted as strings.
- ``model_config = ConfigDict(extra="allow")`` on every model for forward
  compatibility; future fields added by Claude won't break deserialisation.
- ``model_dump(exclude_none=True)`` for serialisation to keep payloads lean.
- Finding IDs are stable within a review (F001, F002, …) and assigned by the
  ReviewResult factory, not by Claude.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ===========================================================================
# PR Context models  (Phase 1 – GitHub API → Claude input)
# ===========================================================================


class RepoInfo(BaseModel):
    """Identifies the repository that owns this PR."""

    model_config = ConfigDict(extra="allow")

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class BranchRef(BaseModel):
    """One side (base or head) of a pull request."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(description="'owner:branch' label as shown on GitHub.")
    ref: str = Field(description="Branch name.")
    sha: str = Field(description="Commit SHA at the tip of this branch.")


class CommitInfo(BaseModel):
    """A single commit included in the PR."""

    model_config = ConfigDict(extra="allow")

    sha: str
    message: str
    author_name: str | None = None
    author_email: str | None = None
    author_date: str | None = None


class ChangedFile(BaseModel):
    """A file that was added, modified, renamed, or deleted in the PR."""

    model_config = ConfigDict(extra="allow")

    path: str = Field(description="File path in the repository.")
    previous_path: str | None = Field(
        default=None,
        description="Former path for renames/moves.",
    )
    status: str = Field(
        description=(
            "GitHub file status: 'added', 'modified', 'removed', "
            "'renamed', 'copied', 'changed', or 'unchanged'."
        )
    )
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = Field(
        default=None,
        description=(
            "Unified diff patch.  None for binary files, files that exceed "
            "GitHub's patch size limit, or files GitHub skips."
        ),
    )
    source_content: str | None = Field(
        default=None,
        description=(
            "Decoded text content of the file at the head SHA.  None when "
            "the file is binary, too large, or the fetch failed."
        ),
    )
    source_fetch_error: str | None = Field(
        default=None,
        description="Non-fatal error message if source content could not be retrieved.",
    )


class PRContext(BaseModel):
    """Complete, normalized context for a pull request review.

    This is the data structure returned by the ``review_pull_request`` MCP
    tool and consumed by Claude.

    Fields are intentionally broader than what Phase 1 uses so that later
    phases can populate them without a schema change.
    """

    model_config = ConfigDict(extra="allow")

    # -- Repository --
    repo: RepoInfo

    # -- Pull request core --
    number: int
    title: str
    body: str | None = Field(
        default=None,
        description="PR description/body (may be None if not provided).",
    )
    state: str = Field(description="'open', 'closed', or 'merged'.")
    author: str | None = Field(
        default=None,
        description="GitHub login of the PR author.",
    )
    created_at: str | None = None
    updated_at: str | None = None
    merged_at: str | None = None

    # -- Branch refs --
    base: BranchRef = Field(description="Target branch (the branch being merged into).")
    head: BranchRef = Field(description="Source branch (the branch containing the changes).")

    # -- Commits --
    commits: list[CommitInfo] = Field(default_factory=list)
    total_commits: int = Field(
        default=0,
        description="Total number of commits as reported by GitHub PR metadata.",
    )

    # -- Changed files --
    files: list[ChangedFile] = Field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    total_changed_files: int = 0

    def to_review_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict suitable for passing to Claude.

        Omits ``None`` values so that the context is as concise as possible.
        """
        return self.model_dump(exclude_none=True)


# ===========================================================================
# Review result enums  (Phase 1 – Claude output contract)
# ===========================================================================


class FindingCategory(str, Enum):
    """The code-review category that a finding belongs to.

    Values are lowercase strings so they serialise cleanly to JSON without
    an extra ``.value`` call (inheriting from ``str`` handles this).
    """

    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    QUALITY = "quality"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    CONCURRENCY = "concurrency"
    MEMORY = "memory"
    TESTING = "testing"


class FindingSeverity(str, Enum):
    """How severe a finding is.

    Ordered conceptually from highest to lowest impact.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingAction(str, Enum):
    """What should happen with this finding when submitting a review.

    INLINE  – post as a line-level comment on the diff.
              Only valid when ``file`` and ``line`` are present and the line
              falls within the PR's changed diff.

    SUMMARY – include in the overall review body comment, not on a specific line.
              Used for architectural notes, broad testing gaps, and findings
              without a valid changed-line anchor.

    DISCARD – the finding was generated but should be suppressed.
              Retained in ReviewResult so Claude can see what was filtered.
    """

    INLINE = "inline"
    SUMMARY = "summary"
    DISCARD = "discard"


# ===========================================================================
# Review result models  (Phase 1 – Claude output contract)
# ===========================================================================

_FINDING_ID_RE = re.compile(r"^F\d{3,}$")


class ReviewFinding(BaseModel):
    """A single code-review finding produced by Claude.

    Claude populates every field.  The MCP server validates the structure
    and assigns ``finding_id`` automatically via ``ReviewResult.from_findings``.

    Extensibility notes (do NOT add until needed):
    - impact: str | None         – narrative impact statement
    - symbol: str | None         – fully-qualified name of affected symbol
    - region: tuple[int,int]|None – (start_line, end_line) code region
    - fingerprint: str | None    – content hash for deduplication across reviews
    - github_comment_id: int|None – populated after submission
    - status: str | None         – "open" | "resolved" | "dismissed"
    """

    model_config = ConfigDict(extra="allow")

    # --- Identity ---
    finding_id: str = Field(
        description=(
            "Stable sequential identifier within this review: F001, F002, … "
            "Assigned by ReviewResult.from_findings; do not set manually."
        ),
    )

    # --- Classification ---
    category: FindingCategory = Field(
        description="Which review category this finding belongs to.",
    )
    severity: FindingSeverity = Field(
        description="How severe the issue is.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Claude's confidence that this is a genuine issue, in [0.0, 1.0]. "
            "0.0 = pure speculation; 1.0 = certain defect. "
            "Use ≥ 0.7 for INLINE, ≥ 0.5 for SUMMARY.  Below 0.5 → DISCARD."
        ),
    )

    # --- Location ---
    file: str | None = Field(
        default=None,
        description=(
            "Repository-relative file path. Required for INLINE action. "
            "Must be one of the files changed in the PR diff."
        ),
    )
    line: int | None = Field(
        default=None,
        description=(
            "Line number in the file at the head SHA. Required for INLINE action. "
            "Must correspond to a line present in the PR diff – do NOT invent "
            "arbitrary line numbers."
        ),
    )

    # --- Content ---
    title: str = Field(
        description="Short, specific title (≤ 80 chars). No generic labels.",
        max_length=200,
    )
    description: str = Field(
        description=(
            "Concise explanation of the problem.  Reference the specific code "
            "construct, variable, or pattern involved."
        ),
    )
    recommendation: str = Field(
        description="Concrete, actionable fix or improvement suggestion.",
    )

    # --- Disposition ---
    action: FindingAction = Field(
        description=(
            "INLINE: post as a diff line comment (requires file + line). "
            "SUMMARY: include in the review body comment. "
            "DISCARD: suppress (retained for traceability)."
        ),
    )

    @field_validator("finding_id")
    @classmethod
    def _validate_finding_id(cls, v: str) -> str:
        if not _FINDING_ID_RE.match(v):
            raise ValueError(
                f"finding_id must match F<digits> (e.g. F001, F042), got: {v!r}"
            )
        return v

    @field_validator("line")
    @classmethod
    def _validate_line_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"line must be a positive integer, got {v}")
        return v

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict, excluding None fields."""
        return self.model_dump(exclude_none=True)


class ReviewSummary(BaseModel):
    """High-level narrative summary of the entire review.

    Claude populates this after producing all findings.
    """

    model_config = ConfigDict(extra="allow")

    verdict: str = Field(
        description=(
            "One-line overall verdict: e.g. 'Approve with minor suggestions' "
            "or 'Request changes – critical correctness issue found'."
        ),
    )
    overview: str = Field(
        description=(
            "2–4 sentence narrative covering the main themes of the review. "
            "Do not repeat individual findings verbatim."
        ),
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="Short bullet points of things done well in this PR.",
    )
    risks: list[str] = Field(
        default_factory=list,
        description=(
            "Short bullet points of the highest-impact concerns.  "
            "Should align with CRITICAL/HIGH severity findings."
        ),
    )


class ReviewResult(BaseModel):
    """The complete structured output of a Claude code review.

    This is the contract between Claude (producer) and the MCP server
    (consumer).  The server validates this model, then uses it to drive
    GitHub API calls (submit_pr_review, post inline comments) in Phase 2.

    Do NOT instantiate directly when findings need stable IDs – use
    ``ReviewResult.from_findings()`` instead.
    """

    model_config = ConfigDict(extra="allow")

    pr_number: int = Field(description="The PR number this review covers.")
    findings: list[ReviewFinding] = Field(
        default_factory=list,
        description="All findings produced by Claude, including DISCARDed ones.",
    )
    summary: ReviewSummary = Field(
        description="High-level narrative summary of the review.",
    )

    # Derived counts – populated by from_findings, read-only after creation.
    total_findings: int = Field(
        default=0,
        description="Total number of findings (all actions).",
    )
    inline_count: int = Field(
        default=0,
        description="Number of findings with action=INLINE.",
    )
    summary_count: int = Field(
        default=0,
        description="Number of findings with action=SUMMARY.",
    )
    discard_count: int = Field(
        default=0,
        description="Number of findings with action=DISCARD.",
    )

    @classmethod
    def from_findings(
        cls,
        pr_number: int,
        findings: list[ReviewFinding],
        summary: ReviewSummary,
    ) -> "ReviewResult":
        """Create a ReviewResult with stable, sequential finding IDs.

        IDs are assigned in the order findings are provided: F001, F002, …

        Args:
            pr_number: The PR number being reviewed.
            findings:  List of ReviewFinding objects *without* finding_id set
                       (or with placeholder IDs – they will be overwritten).
            summary:   The high-level review summary.

        Returns:
            A ReviewResult with all finding_ids normalised to F001, F002, …
        """
        numbered: list[ReviewFinding] = []
        for i, f in enumerate(findings, start=1):
            fid = f"F{i:03d}"
            # Use model_copy to avoid mutating the caller's objects.
            numbered.append(f.model_copy(update={"finding_id": fid}))

        inline = sum(1 for f in numbered if f.action == FindingAction.INLINE)
        in_summary = sum(1 for f in numbered if f.action == FindingAction.SUMMARY)
        discard = sum(1 for f in numbered if f.action == FindingAction.DISCARD)

        return cls(
            pr_number=pr_number,
            findings=numbered,
            summary=summary,
            total_findings=len(numbered),
            inline_count=inline,
            summary_count=in_summary,
            discard_count=discard,
        )

    def active_findings(self) -> list[ReviewFinding]:
        """Return only findings that will appear in the GitHub review (not DISCARDed)."""
        return [f for f in self.findings if f.action != FindingAction.DISCARD]

    def inline_findings(self) -> list[ReviewFinding]:
        """Return only INLINE findings."""
        return [f for f in self.findings if f.action == FindingAction.INLINE]

    def summary_findings(self) -> list[ReviewFinding]:
        """Return only SUMMARY findings."""
        return [f for f in self.findings if f.action == FindingAction.SUMMARY]

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict, excluding None fields."""
        return self.model_dump(exclude_none=True)
