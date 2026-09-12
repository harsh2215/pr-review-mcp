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
# GitHub hard limit for paginated list endpoints.
_MAX_PER_PAGE = 100


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

    def _paginate(self, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all pages of a list endpoint.

        GitHub paginates list responses via the ``Link`` header.  This method
        follows ``next`` links until exhausted, accumulating all items into a
        single list.

        Args:
            path: URL path relative to *base_url*.
            **kwargs: Additional request parameters (e.g. ``params``).

        Returns:
            All items collected across every page.

        Raises:
            GitHubHTTPError: On any non-2xx response.
        """
        params = dict(kwargs.pop("params", {}) or {})
        params.setdefault("per_page", _MAX_PER_PAGE)
        params["per_page"] = min(int(params["per_page"]), _MAX_PER_PAGE)

        results: list[dict[str, Any]] = []
        # current_path is always a relative path for the base_url-configured client.
        current_path: str = path
        current_params: dict | None = params
        first_page = True

        while current_path is not None:
            if first_page:
                response = self._get_client().request("GET", current_path, params=current_params, **kwargs)
                first_page = False
            else:
                response = self._get_client().request("GET", current_path, **kwargs)

            if response.is_error:
                try:
                    body = response.json()
                    message = body.get("message", response.text)
                except Exception:
                    message = response.text or f"HTTP {response.status_code}"
                raise GitHubHTTPError(response.status_code, message)

            page_data = response.json()
            if isinstance(page_data, list):
                results.extend(page_data)

            # Parse Link header – returns absolute URL; strip to relative path+query.
            next_abs_url = _parse_next_link(response.headers.get("link", ""))
            if next_abs_url is None:
                break
            # Convert absolute URL to path+query so the base_url client handles it.
            current_path = _abs_to_relative(next_abs_url, self._base_url)
            current_params = None  # params already encoded in the URL

        return results

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
        """Fetch ALL files changed in a pull request, handling pagination.

        See: https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files

        GitHub paginates this endpoint (max 100 per page).  This method
        follows all pages and returns a single flat list.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            per_page: Items per page (capped at 100).

        Returns:
            All file objects across every page.  Each item contains at minimum:
            ``filename``, ``status``, ``additions``, ``deletions``,
            ``changes``.  The ``patch`` key is present only for text files
            small enough for GitHub to include it.

        Raises:
            GitHubHTTPError: On API errors.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            params={"per_page": min(per_page, _MAX_PER_PAGE)},
        )

    def get_pull_request_commits(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch ALL commits included in a pull request, handling pagination.

        See: https://docs.github.com/en/rest/pulls/pulls#list-commits-on-a-pull-request

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            per_page: Items per page (capped at 100).

        Returns:
            All commit objects across every page.

        Raises:
            GitHubHTTPError: On API errors.
        """
        return self._paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            params={"per_page": min(per_page, _MAX_PER_PAGE)},
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


# ---------------------------------------------------------------------------
# Link header parsing
# ---------------------------------------------------------------------------


def _parse_next_link(link_header: str) -> str | None:
    """Extract the ``next`` URL from a GitHub ``Link`` response header.

    GitHub uses the standard RFC 5988 ``Link`` header:
        ``<https://api.github.com/...?page=2>; rel="next", ...``

    Args:
        link_header: The raw value of the ``Link`` header (may be empty).

    Returns:
        The URL for the next page, or ``None`` if there is no next page.
    """
    if not link_header:
        return None

    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            # Extract the URL from angle brackets.
            url_part = part.split(";")[0].strip()
            if url_part.startswith("<") and url_part.endswith(">"):
                return url_part[1:-1]
    return None


def _abs_to_relative(absolute_url: str, base_url: str) -> str:
    """Convert an absolute GitHub API URL to a path+query string.

    When an httpx ``Client`` is configured with a ``base_url``, it expects
    relative paths, not absolute URLs.  GitHub Link header ``next`` values are
    always absolute – this function strips the base so the client can merge
    them correctly.

    Args:
        absolute_url: Full URL, e.g. ``https://api.github.com/repos/...?page=2``.
        base_url: The base URL configured on the client (no trailing slash).

    Returns:
        The path+query portion, e.g. ``/repos/...?page=2``.
        Returns *absolute_url* unchanged if it does not start with *base_url*
        (should not normally happen with GitHub's own Link headers).
    """
    base = base_url.rstrip("/")
    if absolute_url.startswith(base):
        remainder = absolute_url[len(base):]
        return remainder if remainder.startswith("/") else "/" + remainder
    return absolute_url
