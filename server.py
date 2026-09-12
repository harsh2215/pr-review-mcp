"""
server.py
---------
MCP server entry point for the PR review tool.

Phase 1: Server skeleton only.
- The ``get_pull_request_context`` tool is stubbed here to prove the import
  chain works end-to-end.
- Review logic and the ``review_pull_request`` tool are NOT implemented in
  this phase (see task specification).

Run with:
    python server.py                  # stdio transport (default for Claude Desktop)
    python server.py --transport sse  # SSE transport
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from github.client import GitHubClient, GitHubAuthError, GitHubHTTPError
from utils.github_url import parse_pr_url, InvalidGitHubPRURL

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("pr-review")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_pull_request_context(pr_url: str) -> dict:
    """Fetch all context needed to review a GitHub Pull Request.

    Returns a dictionary with the PR metadata, list of changed files (with
    diffs), and the commits included in the PR.  The MCP host (Claude) can
    then perform a code review over this data.

    Args:
        pr_url: Full GitHub PR URL, e.g.
                ``https://github.com/owner/repo/pull/42``.

    Returns:
        A dict with keys ``pull_request``, ``files``, and ``commits``.

    Raises:
        ValueError: If the URL is not a valid GitHub PR URL.
        RuntimeError: If the GitHub API request fails.
    """
    try:
        parsed = parse_pr_url(pr_url)
    except InvalidGitHubPRURL as exc:
        raise ValueError(str(exc)) from exc

    try:
        client = GitHubClient()
        pr = client.get_pull_request(parsed.owner, parsed.repo, parsed.number)
        files = client.get_pull_request_files(parsed.owner, parsed.repo, parsed.number)
        commits = client.get_pull_request_commits(parsed.owner, parsed.repo, parsed.number)
    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    except GitHubHTTPError as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "pull_request": pr,
        "files": files,
        "commits": commits,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
