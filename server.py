"""
server.py
---------
MCP server entry point for the PR review tool.

Phase 1 tool: ``review_pull_request``
  - Parses the GitHub PR URL.
  - Fetches PR metadata, commits, changed files (with diffs/source).
  - Returns a normalized PRContext as a JSON-compatible dict.
  - Claude (the MCP host) performs the actual code review over this context.

The MCP server does NOT call any LLM.  Claude is the reviewer.

Run with:
    python server.py                  # stdio transport (default for Claude Desktop)
    python server.py --transport sse  # SSE transport
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from github.client import GitHubAuthError, GitHubClient, GitHubHTTPError
from review.normalizer import build_pr_context
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
