"""
review/models.py
----------------
Normalized domain models for a Pull Request review context.

These models are the stable, cleaned representation of GitHub API data that
Claude uses to perform a code review.  They intentionally do NOT expose the
raw GitHub API response – only the fields needed to reason about a PR.

Design principles:
- All fields optional-where-reasonable to survive missing/partial API data.
- ``model_config = ConfigDict(extra="allow")`` on every model so that future
  GitHub API fields can pass through without breaking existing code.
- ``model_dump(exclude_none=True)`` is used when serialising to Claude so that
  absent values don't clutter the context.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


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

    label: str = Field(
        description="'owner:branch' label as shown on GitHub."
    )
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


# ---------------------------------------------------------------------------
# Top-level PRContext
# ---------------------------------------------------------------------------


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
    state: str = Field(
        description="'open', 'closed', or 'merged'."
    )
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
