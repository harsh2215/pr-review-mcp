"""
tests/test_github_url.py
------------------------
Unit tests for utils.github_url.

All cases run without network access or a GitHub token.
"""

import pytest

from utils.github_url import (
    InvalidGitHubPRURL,
    InvalidGitHubRepoURL,
    ParsedPRURL,
    parse_pr_url,
    parse_repo_url,
)


# ---------------------------------------------------------------------------
# Valid URL cases
# ---------------------------------------------------------------------------


class TestValidPRURLs:
    """parse_pr_url must succeed for well-formed GitHub PR URLs."""

    def test_basic_valid_url(self) -> None:
        result = parse_pr_url("https://github.com/owner/repo/pull/1")
        assert isinstance(result, ParsedPRURL)
        assert result.owner == "owner"
        assert result.repo == "repo"
        assert result.number == 1

    def test_large_pr_number(self) -> None:
        result = parse_pr_url("https://github.com/org/project/pull/99999")
        assert result.number == 99999

    def test_hyphenated_owner_and_repo(self) -> None:
        result = parse_pr_url("https://github.com/my-org/my-repo/pull/42")
        assert result.owner == "my-org"
        assert result.repo == "my-repo"
        assert result.number == 42

    def test_dotted_names(self) -> None:
        result = parse_pr_url("https://github.com/owner.name/repo.name/pull/7")
        assert result.owner == "owner.name"
        assert result.repo == "repo.name"

    def test_underscored_names(self) -> None:
        result = parse_pr_url("https://github.com/under_score/re_po/pull/3")
        assert result.owner == "under_score"
        assert result.repo == "re_po"

    def test_trailing_slash_stripped(self) -> None:
        """Trailing slash should be accepted gracefully."""
        result = parse_pr_url("https://github.com/owner/repo/pull/10/")
        assert result.number == 10

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace should be accepted gracefully."""
        result = parse_pr_url("  https://github.com/owner/repo/pull/5  ")
        assert result.number == 5

    def test_roundtrip_str(self) -> None:
        """str(ParsedPRURL) should return the canonical URL form."""
        url = "https://github.com/owner/repo/pull/123"
        result = parse_pr_url(url)
        assert str(result) == url

    def test_api_path(self) -> None:
        result = parse_pr_url("https://github.com/myorg/myrepo/pull/77")
        assert result.api_path == "repos/myorg/myrepo/pulls/77"

    def test_frozen_dataclass(self) -> None:
        """ParsedPRURL instances must be immutable."""
        result = parse_pr_url("https://github.com/owner/repo/pull/1")
        with pytest.raises((AttributeError, TypeError)):
            result.owner = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Repo URL parsing
# ---------------------------------------------------------------------------

class TestParseRepoUrl:
    def test_valid_repo_urls(self) -> None:
        valid_urls = [
            ("https://github.com/owner/repo", "owner", "repo"),
            ("https://github.com/owner-123/repo_name.test", "owner-123", "repo_name.test"),
            ("https://github.com/owner/repo/", "owner", "repo"),
            ("https://github.com/owner/repo////", "owner", "repo"),
        ]
        for url, expected_owner, expected_repo in valid_urls:
            parsed = parse_repo_url(url)
            assert parsed.owner == expected_owner
            assert parsed.repo == expected_repo

    def test_parsed_repo_url_str(self) -> None:
        parsed = parse_repo_url("https://github.com/owner/repo/")
        assert str(parsed) == "https://github.com/owner/repo"

    def test_invalid_scheme(self) -> None:
        with pytest.raises(ValueError, match="https://"):
            parse_repo_url("http://github.com/owner/repo")

    def test_not_github(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            parse_repo_url("https://gitlab.com/owner/repo")

    def test_missing_repo(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            parse_repo_url("https://github.com/owner")

    def test_extra_path_components(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            parse_repo_url("https://github.com/owner/repo/pull/1")

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            parse_repo_url("")

    def test_none(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            parse_repo_url(None)  # type: ignore

# ---------------------------------------------------------------------------
# Malformed URL cases
# ---------------------------------------------------------------------------


class TestMalformedPRURLs:
    """parse_pr_url must raise InvalidGitHubPRURL for bad inputs."""

    def _assert_invalid(self, url: str, *, reason_fragment: str | None = None) -> InvalidGitHubPRURL:
        with pytest.raises(InvalidGitHubPRURL) as exc_info:
            parse_pr_url(url)
        exc = exc_info.value
        assert exc.url == url.strip().rstrip("/") or exc.url in (url, url.strip())
        if reason_fragment:
            assert reason_fragment.lower() in exc.reason.lower() or reason_fragment.lower() in str(exc).lower()
        return exc

    def test_empty_string(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("")

    def test_http_scheme_rejected(self) -> None:
        exc = self._assert_invalid("http://github.com/owner/repo/pull/1")
        assert "https" in exc.reason.lower()

    def test_missing_pull_segment(self) -> None:
        self._assert_invalid("https://github.com/owner/repo/issues/1")

    def test_pr_number_zero(self) -> None:
        # PR numbers start at 1; 0 is invalid.
        self._assert_invalid("https://github.com/owner/repo/pull/0")

    def test_pr_number_negative(self) -> None:
        self._assert_invalid("https://github.com/owner/repo/pull/-1")

    def test_pr_number_non_numeric(self) -> None:
        self._assert_invalid("https://github.com/owner/repo/pull/abc")

    def test_missing_repo(self) -> None:
        self._assert_invalid("https://github.com/owner/pull/1")

    def test_extra_path_segments(self) -> None:
        self._assert_invalid("https://github.com/owner/repo/pull/1/files")

    def test_no_pr_number(self) -> None:
        self._assert_invalid("https://github.com/owner/repo/pull/")


# ---------------------------------------------------------------------------
# Unsupported / wrong host cases
# ---------------------------------------------------------------------------


class TestUnsupportedURLs:
    """parse_pr_url must reject URLs that are not github.com PR URLs."""

    def test_gitlab_url(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("https://gitlab.com/owner/repo/pull/1")

    def test_bitbucket_url(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("https://bitbucket.org/owner/repo/pull-requests/1")

    def test_arbitrary_url(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("https://example.com/owner/repo/pull/1")

    def test_github_enterprise_url(self) -> None:
        # GitHub Enterprise has a different hostname; we only support github.com.
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("https://github.example.com/owner/repo/pull/1")

    def test_not_a_url(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("owner/repo#1")

    def test_bare_number(self) -> None:
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("42")

    def test_none_raises_type_or_invalid(self) -> None:
        """Passing None should raise InvalidGitHubPRURL (not crash unchecked)."""
        with pytest.raises((InvalidGitHubPRURL, TypeError)):
            parse_pr_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Exception structure
# ---------------------------------------------------------------------------


class TestInvalidGitHubPRURLException:
    """Verify the exception carries structured data."""

    def test_exception_has_url_attribute(self) -> None:
        with pytest.raises(InvalidGitHubPRURL) as exc_info:
            parse_pr_url("https://example.com/bad")
        assert hasattr(exc_info.value, "url")

    def test_exception_has_reason_attribute(self) -> None:
        with pytest.raises(InvalidGitHubPRURL) as exc_info:
            parse_pr_url("https://example.com/bad")
        assert hasattr(exc_info.value, "reason")
        assert exc_info.value.reason  # non-empty

    def test_exception_is_value_error_subclass(self) -> None:
        with pytest.raises(ValueError):
            parse_pr_url("not-a-url")
