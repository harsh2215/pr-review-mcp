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

from typing import Any

from mcp.server.fastmcp import FastMCP

from github.client import GitHubAuthError, GitHubClient, GitHubHTTPError
from review.models import ReviewFinding, ReviewResult, ReviewSummary
from review.normalizer import build_pr_context
from review.submission import build_review_payload
from utils.github_url import InvalidGitHubPRURL, parse_pr_url

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("pr-review")

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

    return ctx.to_review_dict()


@mcp.tool()
def submit_pr_review(
    pr_url: str,
    findings: list[ReviewFinding],
    summary: ReviewSummary,
    dry_run: bool = True,
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

    Returns:
        A dict with keys:
        - dry_run (bool), pr_number (int), head_sha (str)
        - inline_findings, summary_findings, discarded_findings
        - review_body (str), inline_comments (list)
        - counts: {inline, summary, discarded}
        - submitted (bool), github_review_id (int | None)

    Raises:
        ValueError: pr_url invalid, or findings/summary fail schema validation.
        RuntimeError: GitHub API unreachable or returns an error.
    """
    # -- 1. Parse PR URL --
    try:
        parsed = parse_pr_url(pr_url)
    except InvalidGitHubPRURL as exc:
        raise ValueError(str(exc)) from exc

    # -- 2. Assign stable IDs (FastMCP already validated Pydantic types) --
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
                event="COMMENT",
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

if __name__ == "__main__":
    mcp.run()
