"""
review/normalizer.py
--------------------
Transforms raw GitHub REST API responses into a clean PRContext.

This module is the boundary between GitHub API data and MCP tool output.
All API-specific field names are handled here; everything above this layer
works with PRContext objects.

Key responsibilities:
- Map GitHub API field names to our domain model.
- Handle missing/optional fields gracefully (binary files, large diffs, etc.).
- Fetch full source content for changed text files at the head SHA.
- Never raise for a single missing file's content – log the error and continue.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from github.client import GitHubClient, GitHubHTTPError
from review.models import BranchRef, ChangedFile, CommitInfo, PRContext, RepoInfo

logger = logging.getLogger(__name__)

# Files larger than this many bytes are considered too large to include as
# decoded source (avoids sending megabytes to Claude).
_MAX_SOURCE_BYTES = 100_000

# GitHub omits patch for files beyond ~1 MB or binary files.  We don't
# fetch source content for files that are "removed" (they no longer exist
# at head) or status types that don't make sense to fetch.
_NO_SOURCE_STATUSES = frozenset({"removed"})


def build_pr_context(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    fetch_source: bool = True,
) -> PRContext:
    """Fetch all PR data and return a normalised PRContext.

    Args:
        client: Authenticated GitHub REST client.
        owner: Repository owner login.
        repo: Repository name.
        pr_number: Pull request number.
        fetch_source: When True (default) attempt to fetch the decoded text
            content of each changed file at the head SHA.  Set to False in
            tests that only need metadata.

    Returns:
        A fully populated :class:`PRContext`.

    Raises:
        GitHubHTTPError: If the PR metadata or file/commit listing fails.
            A single file's source fetch failing does NOT raise – the error
            is captured in ``ChangedFile.source_fetch_error``.
    """
    # 1. Fetch PR metadata (single object – no pagination needed).
    raw_pr = client.get_pull_request(owner, repo, pr_number)

    # 2. Fetch all files and commits (paginated automatically by client).
    raw_files = client.get_pull_request_files(owner, repo, pr_number)
    raw_commits = client.get_pull_request_commits(owner, repo, pr_number)

    # 3. Build the head SHA for source fetching.
    head_sha: str = raw_pr.get("head", {}).get("sha", "")

    # 4. Normalise files.
    changed_files: list[ChangedFile] = []
    for raw_file in raw_files:
        cf = _normalise_file(raw_file)

        if fetch_source and head_sha and cf.status not in _NO_SOURCE_STATUSES:
            cf = _enrich_with_source(client, owner, repo, cf, head_sha)

        changed_files.append(cf)

    # 5. Normalise commits.
    commits = [_normalise_commit(c) for c in raw_commits]

    # 6. Assemble PRContext.
    base_raw = raw_pr.get("base", {})
    head_raw = raw_pr.get("head", {})

    return PRContext(
        repo=RepoInfo(
            owner=owner,
            name=repo,
        ),
        number=raw_pr["number"],
        title=raw_pr.get("title", ""),
        body=raw_pr.get("body"),  # May be None
        state=_resolve_state(raw_pr),
        author=raw_pr.get("user", {}).get("login"),
        created_at=raw_pr.get("created_at"),
        updated_at=raw_pr.get("updated_at"),
        merged_at=raw_pr.get("merged_at"),
        base=BranchRef(
            label=base_raw.get("label", ""),
            ref=base_raw.get("ref", ""),
            sha=base_raw.get("sha", ""),
        ),
        head=BranchRef(
            label=head_raw.get("label", ""),
            ref=head_raw.get("ref", ""),
            sha=head_raw.get("sha", ""),
        ),
        commits=commits,
        total_commits=raw_pr.get("commits", len(commits)),
        files=changed_files,
        total_additions=raw_pr.get("additions", sum(f.additions for f in changed_files)),
        total_deletions=raw_pr.get("deletions", sum(f.deletions for f in changed_files)),
        total_changed_files=raw_pr.get("changed_files", len(changed_files)),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_state(raw_pr: dict[str, Any]) -> str:
    """Derive a three-way state from GitHub's two-way state + merged flag."""
    if raw_pr.get("merged"):
        return "merged"
    return raw_pr.get("state", "unknown")


def _normalise_commit(raw: dict[str, Any]) -> CommitInfo:
    """Map a raw GitHub commit object to a CommitInfo."""
    commit_data = raw.get("commit", {})
    author_data = commit_data.get("author", {})
    return CommitInfo(
        sha=raw.get("sha", ""),
        message=commit_data.get("message", ""),
        author_name=author_data.get("name"),
        author_email=author_data.get("email"),
        author_date=author_data.get("date"),
    )


def _normalise_file(raw: dict[str, Any]) -> ChangedFile:
    """Map a raw GitHub file object to a ChangedFile.

    The ``patch`` field is optional – GitHub omits it for binary files and
    files exceeding the diff size limit.
    """
    return ChangedFile(
        path=raw.get("filename", ""),
        previous_path=raw.get("previous_filename"),
        status=raw.get("status", ""),
        additions=raw.get("additions", 0),
        deletions=raw.get("deletions", 0),
        changes=raw.get("changes", 0),
        patch=raw.get("patch"),  # May be absent – that's fine.
    )


def _enrich_with_source(
    client: GitHubClient,
    owner: str,
    repo: str,
    cf: ChangedFile,
    head_sha: str,
) -> ChangedFile:
    """Attempt to fetch decoded source for a ChangedFile at the head SHA.

    Returns a new ChangedFile with ``source_content`` or
    ``source_fetch_error`` populated.  Never raises.
    """
    try:
        raw_content = client.get_file_contents(owner, repo, cf.path, ref=head_sha)
    except GitHubHTTPError as exc:
        logger.debug("Source fetch failed for %s: %s", cf.path, exc.message)
        return cf.model_copy(
            update={"source_fetch_error": f"GitHub {exc.status_code}: {exc.message}"}
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Source fetch failed for %s: %s", cf.path, exc)
        return cf.model_copy(update={"source_fetch_error": str(exc)})

    encoding = raw_content.get("encoding", "")
    raw_b64 = raw_content.get("content", "")

    if encoding != "base64" or not raw_b64:
        # Could be a submodule reference or unsupported encoding.
        return cf.model_copy(
            update={"source_fetch_error": f"Unsupported encoding: {encoding!r}"}
        )

    try:
        decoded_bytes = base64.b64decode(raw_b64)
    except Exception as exc:  # noqa: BLE001
        return cf.model_copy(update={"source_fetch_error": f"Base64 decode failed: {exc}"})

    if len(decoded_bytes) > _MAX_SOURCE_BYTES:
        return cf.model_copy(
            update={
                "source_fetch_error": (
                    f"File too large to include ({len(decoded_bytes)} bytes > "
                    f"{_MAX_SOURCE_BYTES} limit)"
                )
            }
        )

    try:
        text = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file – treat like binary.
        return cf.model_copy(
            update={"source_fetch_error": "Binary file – cannot decode as UTF-8"}
        )

    return cf.model_copy(update={"source_content": text})
