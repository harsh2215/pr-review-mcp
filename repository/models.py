"""
repository/models.py
--------------------
Pydantic models representing repository-level context returned by the
``get_repository_context`` MCP tool.

These models are read-only representations of GitHub data; they do not
drive any GitHub mutation.

Design notes:
- ``TreeEntry`` is intentionally minimal — path + type only.
  Sha, size, and url are excluded to keep context compact.
- ``model_config = ConfigDict(extra="allow")`` on every model for
  forward compatibility.
- ``model_dump(exclude_none=True)`` for lean JSON serialisation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TreeEntry(BaseModel):
    """A single entry in the repository Git tree.

    Attributes:
        path: Repository-relative file or directory path.
        entry_type: ``"blob"`` for a file, ``"tree"`` for a directory.
    """

    model_config = ConfigDict(extra="allow")

    path: str = Field(description="Repository-relative path of this entry.")
    entry_type: str = Field(
        description="'blob' for a file, 'tree' for a directory.",
        alias="type",
    )

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "type": self.entry_type}


class RepositoryContext(BaseModel):
    """Structured, concise context for a GitHub repository.

    Returned by the ``get_repository_context`` MCP tool and consumed by
    Claude to understand a repository's structure before reviewing a PR.

    No file contents are included — only paths and types.
    """

    model_config = ConfigDict(extra="allow")

    # Repository identity
    owner: str = Field(description="GitHub owner (user or organisation).")
    repo: str = Field(description="Repository name.")
    full_name: str = Field(description="'owner/repo' as a single string.")

    # Metadata
    description: str | None = Field(
        default=None,
        description="Repository description, if set.",
    )
    default_branch: str = Field(
        description="The repository's default branch (e.g. 'main').",
    )
    private: bool = Field(
        default=False,
        description="True if the repository is private.",
    )

    # Selected ref
    ref: str = Field(
        description=(
            "The ref (branch name, tag, or commit SHA) whose tree was fetched. "
            "Equals default_branch when no ref was requested."
        ),
    )
    commit_sha: str = Field(
        description="The commit SHA at the tip of the selected ref.",
    )
    tree_sha: str = Field(
        description="The root tree SHA for the selected commit.",
    )
    tree_truncated: bool = Field(
        default=False,
        description=(
            "True when the repository is too large for GitHub to return the "
            "complete tree in one request.  Partial results are still included."
        ),
    )

    # Tree
    tree: list[TreeEntry] = Field(
        default_factory=list,
        description="All entries in the repository tree (path + type).",
    )

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict suitable for returning from the MCP tool."""
        d = self.model_dump(exclude_none=True)
        # Re-serialise tree entries using their own to_dict for clean output.
        d["tree"] = [e.to_dict() for e in self.tree]
        return d
