"""
utils/github_url.py
-------------------
Parse GitHub Pull Request and Repository URLs into structured components.

Supports exactly:
    PR:   https://github.com/<owner>/<repo>/pull/<number>
    Repo: https://github.com/<owner>/<repo>

Raises InvalidGitHubPRURL / InvalidGitHubRepoURL for anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Pull Request URL
# ---------------------------------------------------------------------------

# Compiled once at import time.
_PR_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.\-]+)"
    r"/"
    r"(?P<repo>[A-Za-z0-9_.\-]+)"
    r"/pull/"
    r"(?P<number>[1-9][0-9]*)$"
)


class InvalidGitHubPRURL(ValueError):
    """Raised when a URL does not match the expected GitHub PR format.

    Attributes:
        url: The original URL that was rejected.
        reason: A human-readable explanation of why it was rejected.
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Invalid GitHub PR URL {url!r}: {reason}")


@dataclass(frozen=True)
class ParsedPRURL:
    """Components extracted from a valid GitHub PR URL.

    All fields are immutable after construction.
    """

    owner: str
    repo: str
    number: int

    @property
    def api_path(self) -> str:
        """Return the GitHub REST API path for this PR (no leading slash)."""
        return f"repos/{self.owner}/{self.repo}/pulls/{self.number}"

    def __str__(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


def parse_pr_url(url: str) -> ParsedPRURL:
    """Parse a GitHub Pull Request URL and return its components.

    Args:
        url: A string of the form
            ``https://github.com/<owner>/<repo>/pull/<number>``.

    Returns:
        A :class:`ParsedPRURL` with ``owner``, ``repo``, and ``number``.

    Raises:
        InvalidGitHubPRURL: If *url* is not a well-formed GitHub PR URL.
            The exception carries both the original URL and a ``reason``
            attribute explaining what is wrong.
    """
    if not url or not isinstance(url, str):
        raise InvalidGitHubPRURL(str(url), "URL must be a non-empty string")

    url = url.strip().rstrip("/")

    if not url.startswith("https://"):
        raise InvalidGitHubPRURL(url, "URL must use the https:// scheme")

    match = _PR_URL_RE.fullmatch(url)
    if match is None:
        raise InvalidGitHubPRURL(
            url,
            "URL must match https://github.com/<owner>/<repo>/pull/<number>",
        )

    return ParsedPRURL(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
    )


# ---------------------------------------------------------------------------
# Repository URL
# ---------------------------------------------------------------------------

_REPO_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.\-]+)"
    r"/"
    r"(?P<repo>[A-Za-z0-9_.\-]+)$"
)


class InvalidGitHubRepoURL(ValueError):
    """Raised when a URL does not match the expected GitHub repository format.

    Attributes:
        url: The original URL that was rejected.
        reason: A human-readable explanation of why it was rejected.
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Invalid GitHub repository URL {url!r}: {reason}")


@dataclass(frozen=True)
class ParsedRepoURL:
    """Components extracted from a valid GitHub repository URL."""

    owner: str
    repo: str

    def __str__(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


def parse_repo_url(url: str) -> ParsedRepoURL:
    """Parse a GitHub repository URL and return its components.

    Accepts:
        ``https://github.com/<owner>/<repo>``

    A trailing slash is stripped before matching.

    Args:
        url: Repository URL string.

    Returns:
        A :class:`ParsedRepoURL` with ``owner`` and ``repo``.

    Raises:
        InvalidGitHubRepoURL: If *url* is not a well-formed GitHub repository URL.
    """
    if not url or not isinstance(url, str):
        raise InvalidGitHubRepoURL(str(url), "URL must be a non-empty string")

    url = url.strip().rstrip("/")

    if not url.startswith("https://"):
        raise InvalidGitHubRepoURL(url, "URL must use the https:// scheme")

    match = _REPO_URL_RE.fullmatch(url)
    if match is None:
        raise InvalidGitHubRepoURL(
            url,
            "URL must match https://github.com/<owner>/<repo>",
        )

    return ParsedRepoURL(
        owner=match.group("owner"),
        repo=match.group("repo"),
    )
