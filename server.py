"""
server.py
---------
MCP server entry point for the PR review tool.

Tools exposed:

``review_pull_request``
  - Parses the GitHub PR URL.
  - Fetches PR metadata, commits, changed files (with diffs/source).
  - Returns a normalized PRContext as a JSON-compatible dict.
  - Claude (the MCP host) performs the actual code review over this context.

``submit_pr_review``
  - Accepts the ReviewResult JSON that Claude produced.
  - Re-fetches the current PR diff from GitHub.
  - Validates every INLINE finding against the actual diff.
  - Classifies findings: INLINE / SUMMARY / DISCARD.
  - dry_run=True  → returns a full preview with no GitHub mutation.
  - dry_run=False → submits ONE GitHub review (body + all inline comments).

The MCP server does NOT call any LLM.  Claude is the reviewer.

Run with:
    python server.py                  # stdio transport (default for Claude Desktop)
    python server.py --transport sse  # SSE transport

Idempotency note:
  Without persistent storage, exactly-once submission cannot be guaranteed
  across client retries.  The server does not implement automatic retries
  that could duplicate reviews.  The caller is responsible for retry
  deduplication if needed.
"""

from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

import argparse
from typing import Any

try:
    # FastMCP
    from fastmcp import FastMCP
except ImportError:
    # MCP v1.x
    from mcp.server.fastmcp import FastMCP

from fastmcp.server.auth.providers.github import GitHubProvider

from github.client import GitHubAuthError, GitHubClient, GitHubHTTPError
from repository.normalizer import (
    build_repository_context,
    build_repository_file,
)
from review.models import ReviewFinding, ReviewResult, ReviewSummary
from review.normalizer import build_pr_context
from review.prompts import get_review_prompt
from review.submission import ReviewEvent, build_review_payload

from utils.github_url import (
    InvalidGitHubPRURL,
    InvalidGitHubRepoURL,
    parse_pr_url,
    parse_repo_url,
)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

client_id=os.environ.get("GITHUB_CLIENT_ID")
client_secret=os.environ.get("GITHUB_CLIENT_SECRET")

auth = None
if client_id and client_secret:
    auth = GitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url="https://pr-review-mcp.onrender.com",
        redirect_path="/oauth/github/callback",
    )

mcp = FastMCP("pr-review", auth=auth)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def review_pull_request(pr_url: str) -> dict:
    """Retrieve a GitHub Pull Request and return normalized context for review.

    This tool collects everything Claude needs to perform a thorough code
    review of a pull request:
    - PR metadata (title, description, state, author, timestamps)
    - Base and head branch refs with their commit SHAs
    - All commits included in the PR
    - All changed files with unified diffs (where available)
    - Full source content of changed text files at the head SHA

    The MCP host (Claude) is responsible for the actual code review; this tool
    only fetches and normalizes data.

    Args:
        pr_url: Full GitHub PR URL.
            Example: ``https://github.com/owner/repo/pull/42``

    Returns:
        A JSON-compatible dict representing a ``PRContext``.  Top-level keys:

        - ``repo``: ``{owner, name}``
        - ``number``: PR number (int)
        - ``title``: PR title
        - ``body``: PR description (may be absent)
        - ``state``: ``"open"``, ``"closed"``, or ``"merged"``
        - ``author``: GitHub login of the PR author
        - ``base``: ``{label, ref, sha}`` – target branch
        - ``head``: ``{label, ref, sha}`` – source branch
        - ``commits``: list of ``{sha, message, author_name, ...}``
        - ``total_commits``: int
        - ``files``: list of ``{path, status, additions, deletions, patch, source_content, ...}``
        - ``total_additions``, ``total_deletions``, ``total_changed_files``

    Raises:
        ValueError: If *pr_url* is not a valid GitHub PR URL.
        RuntimeError: If the GitHub API is unreachable or returns an error.
    """
    # -- URL parsing --
    try:
        parsed = parse_pr_url(pr_url)
    except InvalidGitHubPRURL as exc:
        raise ValueError(str(exc)) from exc

    # -- GitHub fetch + normalization --
    try:
        with GitHubClient() as client:
            ctx = build_pr_context(
                client,
                parsed.owner,
                parsed.repo,
                parsed.number,
                fetch_source=True,
            )
    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    except GitHubHTTPError as exc:
        raise RuntimeError(str(exc)) from exc

    # -- Assemble the result: PR context + full review rubric --
    result = ctx.to_review_dict()
    result["review_instructions"] = get_review_prompt()
    return result


@mcp.tool()
def get_repository_context(repo_url: str, ref: str | None = None) -> dict[str, Any]:
    """Retrieve structured repository-level context for a GitHub repository.

    This tool helps Claude understand a repository's structure and metadata
    without fetching full file contents. It returns the repository default
    branch, the commit SHA of the requested ref, and the complete Git tree
    (file and directory paths).

    Args:
        repo_url: Full GitHub repository URL.
                  Example: ``https://github.com/owner/repo``
        ref:      Optional branch name, tag, or commit SHA. If omitted, the
                  repository's default branch is used.

    Returns:
        A JSON-compatible dict representing a ``RepositoryContext``. Keys:
        - ``owner``, ``repo``, ``full_name``, ``description``
        - ``default_branch``, ``private``
        - ``ref``, ``commit_sha``, ``tree_sha``, ``tree_truncated``
        - ``tree``: list of ``{"path": str, "type": "blob" | "tree"}``

    Raises:
        ValueError: If *repo_url* is not a valid GitHub repository URL, or
                    if the repository/ref cannot be found.
        RuntimeError: If the GitHub API is unreachable or returns an error.
    """
    try:
        parsed = parse_repo_url(repo_url)
    except InvalidGitHubRepoURL as exc:
        raise ValueError(str(exc)) from exc

    try:
        with GitHubClient() as client:
            ctx = build_repository_context(
                client,
                parsed.owner,
                parsed.repo,
                ref=ref,
            )
    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    except GitHubHTTPError as exc:
        raise RuntimeError(str(exc)) from exc

    return ctx.to_dict()


@mcp.tool()
def get_repository_file(
    repo_url: str,
    path: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Retrieve the contents of a single file from a GitHub repository.

    Use this tool after exploring the repository structure with
    `get_repository_context` to fetch specific files of interest.

    Args:
        repo_url: Full GitHub repository URL.
                  Example: ``https://github.com/owner/repo``
        path:     Repository-relative path to the file.
        ref:      Optional branch name, tag, or commit SHA. If omitted, the
                  repository's default branch is used.

    Returns:
        A JSON-compatible dict containing:
        - ``path``, ``ref``, ``size``
        - ``content``: UTF-8 decoded text content (null if binary).
        - ``is_binary``: True if the file could not be decoded as text.
        - ``is_truncated``: True if the file exceeded the 100KB size limit.

    Raises:
        ValueError: If *repo_url* is invalid, or if *path* points to a directory.
        RuntimeError: If the GitHub API returns an error (e.g. 404 Not Found).
    """
    try:
        parsed = parse_repo_url(repo_url)
    except InvalidGitHubRepoURL as exc:
        raise ValueError(str(exc)) from exc

    try:
        with GitHubClient() as client:
            file_ctx = build_repository_file(
                client,
                parsed.owner,
                parsed.repo,
                path,
                ref=ref,
            )
    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    except GitHubHTTPError as exc:
        raise RuntimeError(str(exc)) from exc

    return file_ctx.to_dict()


@mcp.tool()
def submit_pr_review(
    pr_url: str,
    findings: list[ReviewFinding],
    summary: ReviewSummary,
    dry_run: bool = True,
    event: str = "COMMENT",
) -> dict[str, Any]:
    """Validate and submit a structured PR code review produced by Claude.

    Call this after ``review_pull_request`` once you have analysed the PR.

    **Schema is enforced by Pydantic.** The MCP framework exposes the full
    ``ReviewFinding`` and ``ReviewSummary`` JSON schemas automatically;
    invalid enum values, out-of-range confidence, or missing required fields
    are rejected before any GitHub interaction.

    finding_id is assigned automatically (F001, F002, …); you may pass any
    placeholder or omit it — the server overwrites it.

    Inline findings (action="inline") MUST reference an actual changed line
    in the PR diff (file + line present in the unified diff).  Lines not in
    the diff are downgraded to action="summary" automatically.

    Args:
        pr_url:   Full GitHub PR URL (same as passed to ``review_pull_request``).
        findings: List of ReviewFinding objects — see ReviewFinding schema for
                  required/optional fields, enum values, and confidence range.
        summary:  ReviewSummary — see ReviewSummary schema for required fields.
        dry_run:  When True (default) validate and preview with NO GitHub
                  mutation.  When False, post the review to GitHub.
        event:    GitHub review event. Allowed values:
                  - "COMMENT" (default) — submit review comments without
                    requesting changes or approving.
                  - "REQUEST_CHANGES" — indicate that changes are required
                    before this PR can be merged.
                  The value is validated before any GitHub mutation.
                  APPROVE is not supported in this version.

    Returns:
        A dict with keys:
        - dry_run (bool), pr_number (int), head_sha (str), event (str)
        - inline_findings, summary_findings, discarded_findings
        - review_body (str), inline_comments (list)
        - counts: {inline, summary, discarded}
        - submitted (bool), github_review_id (int | None)

    Raises:
        ValueError: pr_url invalid, findings/summary fail schema validation,
                    or event is not an allowed value.
        RuntimeError: GitHub API unreachable or returns an error.
    """
    # -- 1. Parse PR URL --
    try:
        parsed = parse_pr_url(pr_url)
    except InvalidGitHubPRURL as exc:
        raise ValueError(str(exc)) from exc

    # -- 2. Validate event (before any GitHub interaction) --
    try:
        validated_event = ReviewEvent.validate(event)
    except ValueError:
        raise

    # -- 3. Assign stable IDs (FastMCP already validated Pydantic types) --
    review_result = ReviewResult.from_findings(
        pr_number=parsed.number,
        findings=findings,
        summary=summary,
    )

    # -- 3. Re-fetch current PR context from GitHub (all GET, read-only) --
    try:
        with GitHubClient() as client:
            ctx = build_pr_context(
                client,
                parsed.owner,
                parsed.repo,
                parsed.number,
                fetch_source=False,   # diffs are all we need for line validation
            )

            # -- 4. Build payload (diff validation + classification) --
            payload = build_review_payload(
                result=review_result,
                pr_context_files=ctx.files,
                head_sha=ctx.head.sha,
                event=validated_event,
            )

            # -- 5. dry_run=True: return preview, no mutation --
            if dry_run:
                result_dict = payload.to_dict()
                result_dict["dry_run"] = True
                result_dict["submitted"] = False
                result_dict["github_review_id"] = None
                return result_dict

            # -- 6. dry_run=False: submit ONE review --
            github_response = client.post_review(
                parsed.owner,
                parsed.repo,
                parsed.number,
                commit_id=payload.head_sha,
                body=payload.review_body,
                event=validated_event,
                comments=payload.inline_comments if payload.inline_comments else None,
            )

    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    except GitHubHTTPError as exc:
        raise RuntimeError(str(exc)) from exc

    result_dict = payload.to_dict()
    result_dict["dry_run"] = False
    result_dict["submitted"] = True
    result_dict["github_review_id"] = github_response.get("id")
    return result_dict


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

app = mcp.http_app(transport="http")

def main(transport: str = "http"):
    parser = argparse.ArgumentParser(description="Run the PR review MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "http"),
        default=transport,
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
        )


if __name__ == "__main__":
    main()
