"""
github/client.py
----------------
Reusable GitHub REST API client authenticated with a personal access token.

Security invariants enforced here:
- The token is read **once** from the environment and stored only in the
  Authorization header value.  It is never logged, printed, included in
  exception messages, or propagated into MCP responses.
- Authorization headers are excluded from all debug representations.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitHubAuthError(RuntimeError):
    """Raised when no GitHub token is available."""


class GitHubHTTPError(RuntimeError):
    """Raised for non-2xx responses from the GitHub REST API.

    Attributes:
        status_code: The HTTP status code returned by GitHub.
        message: GitHub's error message (from the response body when available).
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API error {status_code}: {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _load_token() -> str:
    """Load the GitHub token from environment variables.

    Reads from .env if not already present in the environment.

    Returns:
        The token string.

    Raises:
        GitHubAuthError: If GITHUB_TOKEN is missing or empty.
    """
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise GitHubAuthError(
            "GITHUB_TOKEN is not set.  "
            "Add it to your .env file or export it in your shell."
        )
    return token


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin wrapper around the GitHub v3 REST API.

    Instantiate once and reuse for the lifetime of a server request.
    The underlying :class:`httpx.Client` is created lazily on first use so
    that construction is cheap (and testable without a token).

    Args:
        token: GitHub personal access token.  When *None* the token is loaded
               from the ``GITHUB_TOKEN`` environment variable (or ``.env``).
        base_url: Override the GitHub API base URL (useful in tests).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = _GITHUB_API_BASE,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token: str = token if token is not None else _load_token()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Internal HTTP machinery
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "pr-review-mcp/1.0",
                },
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute a request and return the parsed JSON body.

        Args:
            method: HTTP verb (e.g. ``"GET"``).
            path: URL path relative to *base_url* (must start with ``/``).
            **kwargs: Passed directly to :meth:`httpx.Client.request`.

        Returns:
            Parsed JSON – either a ``dict`` or a ``list``.

        Raises:
            GitHubHTTPError: For any non-2xx status code.
            httpx.TimeoutException: If the request exceeds *timeout*.
        """
        client = self._get_client()
        response = client.request(method, path, **kwargs)

        if response.is_error:
            # Extract GitHub's error message safely – never include auth headers.
            try:
                body = response.json()
                message = body.get("message", response.text)
            except Exception:
                message = response.text or f"HTTP {response.status_code}"
            raise GitHubHTTPError(response.status_code, message)

        return response.json()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # High-level GitHub REST API methods
    # ------------------------------------------------------------------

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch metadata for a single pull request.

        See: https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request

        Args:
            owner: Repository owner (user or organisation login).
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            The pull request object as returned by the GitHub REST API.

        Raises:
            GitHubHTTPError: On API errors (e.g. 404 not found, 403 forbidden).
        """
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
        )

    def get_pull_request_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch the list of files changed in a pull request.

        See: https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            per_page: Number of results per page (max 100, GitHub's limit).

        Returns:
            A list of file objects.  Each item contains at minimum:
            ``filename``, ``status``, ``additions``, ``deletions``,
            ``changes``, and ``patch``.

        Raises:
            GitHubHTTPError: On API errors.
        """
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            params={"per_page": min(per_page, 100)},
        )

    def get_pull_request_commits(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch commits included in a pull request.

        See: https://docs.github.com/en/rest/pulls/pulls#list-commits-on-a-pull-request

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            per_page: Number of results per page (max 100).

        Returns:
            A list of commit objects.

        Raises:
            GitHubHTTPError: On API errors.
        """
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            params={"per_page": min(per_page, 100)},
        )

    def get_file_contents(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the contents of a single file from a repository.

        See: https://docs.github.com/en/rest/repos/contents#get-repository-content

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path within the repository (no leading slash).
            ref: Branch, tag, or commit SHA.  Defaults to the repo's default
                 branch when omitted.

        Returns:
            A content object.  The file data is Base64-encoded in the
            ``content`` field.  Use ``base64.b64decode(obj["content"])`` to
            get the raw bytes.

        Raises:
            GitHubHTTPError: On API errors (e.g. 404 if path does not exist).
        """
        params: dict[str, str] = {}
        if ref is not None:
            params["ref"] = ref

        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}",
            params=params,
        )
