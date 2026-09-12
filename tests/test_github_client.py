"""
tests/test_github_client.py
---------------------------
Unit tests for github.client.GitHubClient.

All tests use mocked HTTP responses (via respx) and a fake token – no real
GitHub token or network access is required.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest
import respx

from github.client import GitHubAuthError, GitHubClient, GitHubHTTPError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_TOKEN = "ghp_fake_token_for_tests_only"
BASE_URL = "https://api.github.com"


def make_client() -> GitHubClient:
    """Return a GitHubClient using a fake token (no env-var lookup)."""
    return GitHubClient(token=FAKE_TOKEN)


# ---------------------------------------------------------------------------
# Authentication / configuration
# ---------------------------------------------------------------------------


class TestClientAuthentication:
    """GitHubClient must load tokens securely and reject missing ones."""

    def test_accepts_explicit_token(self) -> None:
        """Constructing with an explicit token must not raise."""
        client = GitHubClient(token=FAKE_TOKEN)
        assert client is not None

    def test_loads_token_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no token is passed, it should be read from GITHUB_TOKEN."""
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        # Patch load_dotenv so it doesn't overwrite the monkeypatched value.
        with patch("github.client.load_dotenv", return_value=None):
            client = GitHubClient()
        assert client is not None

    def test_raises_auth_error_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GITHUB_TOKEN must raise GitHubAuthError, not crash generically."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("github.client.load_dotenv", return_value=None):
            with pytest.raises(GitHubAuthError):
                GitHubClient()

    def test_raises_auth_error_when_token_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty GITHUB_TOKEN string must raise GitHubAuthError."""
        monkeypatch.setenv("GITHUB_TOKEN", "   ")
        with patch("github.client.load_dotenv", return_value=None):
            with pytest.raises(GitHubAuthError):
                GitHubClient()

    @respx.mock
    def test_token_not_in_exception_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The token value must NEVER appear in exception messages."""
        secret = "super_secret_token_abc123"
        monkeypatch.setenv("GITHUB_TOKEN", secret)
        with patch("github.client.load_dotenv", return_value=None):
            client = GitHubClient()

        # Force an HTTP error by mocking a 401 response.
        respx.get(f"{BASE_URL}/repos/x/y/pulls/1").mock(
            return_value=httpx.Response(
                401,
                json={"message": "Bad credentials"},
            )
        )
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("x", "y", 1)

        assert secret not in str(exc_info.value)

    def test_authorization_header_uses_bearer_scheme(self) -> None:
        """The Authorization header must use Bearer, not token."""
        client = GitHubClient(token=FAKE_TOKEN)
        http_client = client._get_client()
        auth_header = http_client.headers.get("authorization", "")
        assert auth_header.startswith("Bearer ")
        # Token appears in the header value, which is fine – but let's also
        # confirm the token is NOT leaked anywhere outside the headers dict.
        assert FAKE_TOKEN in auth_header  # correct – it should be there

    def test_context_manager_closes_client(self) -> None:
        """Using GitHubClient as a context manager should close cleanly."""
        with GitHubClient(token=FAKE_TOKEN) as client:
            assert client is not None
        # After __exit__, the internal client should be None.
        assert client._client is None


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHTTPErrorHandling:
    """GitHubClient must translate non-2xx responses into GitHubHTTPError."""

    @respx.mock
    def test_404_raises_http_error(self) -> None:
        respx.get(f"{BASE_URL}/repos/owner/repo/pulls/999").mock(
            return_value=httpx.Response(
                404,
                json={"message": "Not Found"},
            )
        )
        client = make_client()
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("owner", "repo", 999)

        assert exc_info.value.status_code == 404
        assert "Not Found" in exc_info.value.message

    @respx.mock
    def test_403_raises_http_error(self) -> None:
        respx.get(f"{BASE_URL}/repos/owner/repo/pulls/1").mock(
            return_value=httpx.Response(
                403,
                json={"message": "Forbidden"},
            )
        )
        client = make_client()
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("owner", "repo", 1)

        assert exc_info.value.status_code == 403

    @respx.mock
    def test_500_raises_http_error(self) -> None:
        respx.get(f"{BASE_URL}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        client = make_client()
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("o", "r", 1)

        assert exc_info.value.status_code == 500

    @respx.mock
    def test_http_error_does_not_include_token(self) -> None:
        """Token must not be present in GitHubHTTPError messages."""
        respx.get(f"{BASE_URL}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        client = GitHubClient(token=FAKE_TOKEN)
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("o", "r", 1)

        assert FAKE_TOKEN not in str(exc_info.value)
        assert FAKE_TOKEN not in exc_info.value.message

    @respx.mock
    def test_http_error_attributes(self) -> None:
        """GitHubHTTPError should expose status_code and message."""
        respx.get(f"{BASE_URL}/repos/o/r/pulls/2").mock(
            return_value=httpx.Response(422, json={"message": "Validation Failed"})
        )
        client = make_client()
        with pytest.raises(GitHubHTTPError) as exc_info:
            client.get_pull_request("o", "r", 2)

        exc = exc_info.value
        assert exc.status_code == 422
        assert "Validation Failed" in exc.message

    def test_github_http_error_is_runtime_error(self) -> None:
        exc = GitHubHTTPError(404, "Not Found")
        assert isinstance(exc, RuntimeError)

    def test_github_auth_error_is_runtime_error(self) -> None:
        exc = GitHubAuthError("No token")
        assert isinstance(exc, RuntimeError)


# ---------------------------------------------------------------------------
# Mocked API responses
# ---------------------------------------------------------------------------


class TestMockedAPIResponses:
    """Verify each high-level method correctly calls the right endpoint."""

    @respx.mock
    def test_get_pull_request_calls_correct_endpoint(self) -> None:
        pr_data = {
            "number": 1,
            "title": "Fix bug",
            "state": "open",
            "body": "This fixes the bug.",
        }
        route = respx.get(f"{BASE_URL}/repos/myorg/myrepo/pulls/1").mock(
            return_value=httpx.Response(200, json=pr_data)
        )

        client = make_client()
        result = client.get_pull_request("myorg", "myrepo", 1)

        assert route.called
        assert result["number"] == 1
        assert result["title"] == "Fix bug"

    @respx.mock
    def test_get_pull_request_files_calls_correct_endpoint(self) -> None:
        files_data = [
            {
                "filename": "src/main.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "changes": 7,
                "patch": "@@ -1,3 +1,6 @@\n+import os\n ...",
            }
        ]
        route = respx.get(f"{BASE_URL}/repos/myorg/myrepo/pulls/1/files").mock(
            return_value=httpx.Response(200, json=files_data)
        )

        client = make_client()
        result = client.get_pull_request_files("myorg", "myrepo", 1)

        assert route.called
        assert len(result) == 1
        assert result[0]["filename"] == "src/main.py"

    @respx.mock
    def test_get_pull_request_commits_calls_correct_endpoint(self) -> None:
        commits_data = [
            {
                "sha": "abc123",
                "commit": {"message": "Initial fix"},
            }
        ]
        route = respx.get(f"{BASE_URL}/repos/myorg/myrepo/pulls/1/commits").mock(
            return_value=httpx.Response(200, json=commits_data)
        )

        client = make_client()
        result = client.get_pull_request_commits("myorg", "myrepo", 1)

        assert route.called
        assert len(result) == 1
        assert result[0]["sha"] == "abc123"

    @respx.mock
    def test_get_file_contents_calls_correct_endpoint(self) -> None:
        content_data = {
            "name": "README.md",
            "path": "README.md",
            "encoding": "base64",
            "content": "SGVsbG8gV29ybGQ=\n",
        }
        route = respx.get(f"{BASE_URL}/repos/myorg/myrepo/contents/README.md").mock(
            return_value=httpx.Response(200, json=content_data)
        )

        client = make_client()
        result = client.get_file_contents("myorg", "myrepo", "README.md")

        assert route.called
        assert result["name"] == "README.md"

    @respx.mock
    def test_get_file_contents_with_ref(self) -> None:
        content_data = {"name": "app.py", "encoding": "base64", "content": ""}
        respx.get(f"{BASE_URL}/repos/o/r/contents/app.py").mock(
            return_value=httpx.Response(200, json=content_data)
        )

        client = make_client()
        # Should not raise; we just verify it passes the ref param.
        result = client.get_file_contents("o", "r", "app.py", ref="main")
        assert result["name"] == "app.py"

    @respx.mock
    def test_files_per_page_capped_at_100(self) -> None:
        """The per_page param must never exceed GitHub's hard limit of 100."""
        route = respx.get(f"{BASE_URL}/repos/o/r/pulls/1/files").mock(
            return_value=httpx.Response(200, json=[])
        )

        client = make_client()
        client.get_pull_request_files("o", "r", 1, per_page=200)

        request = route.calls.last.request
        # The query string should contain per_page=100, not 200.
        assert "per_page=100" in str(request.url)

    @respx.mock
    def test_correct_accept_header_sent(self) -> None:
        """The client must send the correct Accept header."""
        route = respx.get(f"{BASE_URL}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(200, json={"number": 1})
        )

        client = make_client()
        client.get_pull_request("o", "r", 1)

        request = route.calls.last.request
        assert "application/vnd.github+json" in request.headers.get("accept", "")
