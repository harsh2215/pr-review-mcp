"""
tests/test_review_tool.py
-------------------------
Unit tests for the review_pull_request MCP tool and supporting normalizer.

All tests use mocked HTTP responses – no real GitHub token or network access.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from github.client import GitHubClient, GitHubHTTPError
from github.client import _parse_next_link, _abs_to_relative
from review.models import PRContext, ChangedFile, CommitInfo
from review.normalizer import build_pr_context
from utils.github_url import InvalidGitHubPRURL, parse_pr_url

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "ghp_fake_token_for_unit_tests"
BASE_URL = "https://api.github.com"
OWNER = "testorg"
REPO = "testrepo"
PR_NUM = 7


def make_client() -> GitHubClient:
    return GitHubClient(token=FAKE_TOKEN)


def _b64(text: str) -> str:
    """Base64-encode a string the way GitHub does (with newline)."""
    return base64.b64encode(text.encode()).decode() + "\n"


# Minimal valid PR API response.
FAKE_PR: dict[str, Any] = {
    "number": PR_NUM,
    "title": "Add feature X",
    "body": "This adds feature X.",
    "state": "open",
    "merged": False,
    "user": {"login": "devuser"},
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-02T00:00:00Z",
    "merged_at": None,
    "commits": 2,
    "additions": 10,
    "deletions": 3,
    "changed_files": 1,
    "base": {
        "label": "testorg:main",
        "ref": "main",
        "sha": "base000sha",
    },
    "head": {
        "label": "testorg:feature-x",
        "ref": "feature-x",
        "sha": "head000sha",
    },
}

FAKE_FILES: list[dict[str, Any]] = [
    {
        "filename": "src/feature.py",
        "status": "added",
        "additions": 10,
        "deletions": 0,
        "changes": 10,
        "patch": "@@ -0,0 +1,10 @@\n+def feature_x():\n+    pass",
    }
]

FAKE_COMMITS: list[dict[str, Any]] = [
    {
        "sha": "aaa111",
        "commit": {
            "message": "Initial implementation",
            "author": {
                "name": "Dev User",
                "email": "dev@example.com",
                "date": "2024-01-01T12:00:00Z",
            },
        },
    },
    {
        "sha": "bbb222",
        "commit": {
            "message": "Fix edge case",
            "author": {
                "name": "Dev User",
                "email": "dev@example.com",
                "date": "2024-01-02T08:00:00Z",
            },
        },
    },
]

FAKE_FILE_CONTENT: dict[str, Any] = {
    "name": "feature.py",
    "path": "src/feature.py",
    "encoding": "base64",
    "content": _b64("def feature_x():\n    pass\n"),
}


def _mock_standard_pr(*, with_source: bool = True) -> None:
    """Register respx mocks for a standard successful PR fetch."""
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=FAKE_PR)
    )
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files").mock(
        return_value=httpx.Response(200, json=FAKE_FILES)
    )
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/commits").mock(
        return_value=httpx.Response(200, json=FAKE_COMMITS)
    )
    if with_source:
        respx.get(
            f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/src/feature.py"
        ).mock(return_value=httpx.Response(200, json=FAKE_FILE_CONTENT))


# ---------------------------------------------------------------------------
# Normalisation correctness
# ---------------------------------------------------------------------------


class TestNormalisationCorrectness:
    """build_pr_context must map GitHub fields to PRContext correctly."""

    @respx.mock
    def test_basic_pr_metadata(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert isinstance(ctx, PRContext)
        assert ctx.number == PR_NUM
        assert ctx.title == "Add feature X"
        assert ctx.body == "This adds feature X."
        assert ctx.state == "open"
        assert ctx.author == "devuser"

    @respx.mock
    def test_base_and_head_sha_preserved(self) -> None:
        """Base and head SHAs must be present for future incremental review."""
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert ctx.base.sha == "base000sha"
        assert ctx.head.sha == "head000sha"
        assert ctx.base.ref == "main"
        assert ctx.head.ref == "feature-x"

    @respx.mock
    def test_commits_normalised(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert len(ctx.commits) == 2
        commit = ctx.commits[0]
        assert isinstance(commit, CommitInfo)
        assert commit.sha == "aaa111"
        assert "Initial implementation" in commit.message
        assert commit.author_name == "Dev User"

    @respx.mock
    def test_files_normalised(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert len(ctx.files) == 1
        f = ctx.files[0]
        assert isinstance(f, ChangedFile)
        assert f.path == "src/feature.py"
        assert f.status == "added"
        assert f.additions == 10
        assert f.patch is not None
        assert "@@ -0,0 +1,10 @@" in f.patch

    @respx.mock
    def test_repo_info(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert ctx.repo.owner == OWNER
        assert ctx.repo.name == REPO

    @respx.mock
    def test_total_counts(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert ctx.total_commits == 2
        assert ctx.total_additions == 10
        assert ctx.total_deletions == 3
        assert ctx.total_changed_files == 1

    @respx.mock
    def test_merged_state_resolved(self) -> None:
        """A merged PR must report state='merged', not 'closed'."""
        merged_pr = {**FAKE_PR, "state": "closed", "merged": True, "merged_at": "2024-01-03T00:00:00Z"}
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
            return_value=httpx.Response(200, json=merged_pr)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files").mock(
            return_value=httpx.Response(200, json=FAKE_FILES)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/commits").mock(
            return_value=httpx.Response(200, json=FAKE_COMMITS)
        )

        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)
        assert ctx.state == "merged"
        assert ctx.merged_at == "2024-01-03T00:00:00Z"

    @respx.mock
    def test_to_review_dict_excludes_none(self) -> None:
        _mock_standard_pr()
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)
        d = ctx.to_review_dict()

        # 'body' is present in FAKE_PR, so it should appear.
        assert "body" in d
        # 'merged_at' is None in FAKE_PR, so it should be excluded.
        assert "merged_at" not in d


# ---------------------------------------------------------------------------
# Source content fetching
# ---------------------------------------------------------------------------


class TestSourceContentFetching:
    """Source content should be decoded and attached to ChangedFile."""

    @respx.mock
    def test_source_content_decoded(self) -> None:
        _mock_standard_pr(with_source=True)
        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)

        f = ctx.files[0]
        assert f.source_content is not None
        assert "def feature_x" in f.source_content
        assert f.source_fetch_error is None

    @respx.mock
    def test_source_fetch_404_captured_gracefully(self) -> None:
        """A 404 on source fetch must NOT fail the entire build_pr_context."""
        _mock_standard_pr(with_source=False)
        respx.get(
            f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/src/feature.py"
        ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)

        f = ctx.files[0]
        assert f.source_content is None
        assert f.source_fetch_error is not None
        assert "404" in f.source_fetch_error

    @respx.mock
    def test_removed_file_not_fetched(self) -> None:
        """Deleted files should not have source fetched (they no longer exist at head)."""
        removed_files = [
            {**FAKE_FILES[0], "status": "removed"}
        ]
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
            return_value=httpx.Response(200, json=FAKE_PR)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files").mock(
            return_value=httpx.Response(200, json=removed_files)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/commits").mock(
            return_value=httpx.Response(200, json=FAKE_COMMITS)
        )
        # No contents mock registered – if a fetch is attempted it would fail.

        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)
        f = ctx.files[0]
        assert f.source_content is None
        assert f.source_fetch_error is None

    @respx.mock
    def test_binary_file_graceful(self) -> None:
        """A file with unsupported encoding must set source_fetch_error, not raise."""
        binary_content = {
            "name": "image.png",
            "path": "src/feature.py",  # reusing the path from FAKE_FILES
            "encoding": "none",
            "content": "",
        }
        _mock_standard_pr(with_source=False)
        respx.get(
            f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/src/feature.py"
        ).mock(return_value=httpx.Response(200, json=binary_content))

        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)
        f = ctx.files[0]
        assert f.source_content is None
        assert f.source_fetch_error is not None


# ---------------------------------------------------------------------------
# Missing patch handling
# ---------------------------------------------------------------------------


class TestMissingPatchHandling:
    """Files without a patch must still be included; the tool must not fail."""

    @respx.mock
    def test_file_without_patch_included(self) -> None:
        """A binary file with no patch must appear in files list with patch=None."""
        files_no_patch = [
            {
                "filename": "assets/logo.png",
                "status": "added",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                # No "patch" key – GitHub omits it for binary files.
            }
        ]
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
            return_value=httpx.Response(200, json=FAKE_PR)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files").mock(
            return_value=httpx.Response(200, json=files_no_patch)
        )
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/commits").mock(
            return_value=httpx.Response(200, json=FAKE_COMMITS)
        )
        # Source fetch for png will get a 404:
        respx.get(
            f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/assets/logo.png"
        ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

        ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)

        assert len(ctx.files) == 1
        f = ctx.files[0]
        assert f.path == "assets/logo.png"
        assert f.patch is None
        # source_fetch_error is expected because 404.
        assert f.source_content is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """build_pr_context must surface GitHub errors cleanly."""

    @respx.mock
    def test_pr_not_found_raises(self) -> None:
        """A 404 on the PR itself must propagate as GitHubHTTPError."""
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/999").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with pytest.raises(GitHubHTTPError) as exc_info:
            build_pr_context(make_client(), OWNER, REPO, 999, fetch_source=False)

        assert exc_info.value.status_code == 404

    @respx.mock
    def test_github_api_failure_raises(self) -> None:
        """A 500 from GitHub must propagate as GitHubHTTPError."""
        respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(GitHubHTTPError) as exc_info:
            build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=False)

        assert exc_info.value.status_code == 500

    def test_malformed_url_raises_value_error(self) -> None:
        """An invalid PR URL must raise InvalidGitHubPRURL (which is a ValueError)."""
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("not-a-github-url")

    def test_non_pr_github_url_raises(self) -> None:
        """A GitHub URL for an issue (not a PR) must be rejected."""
        with pytest.raises(InvalidGitHubPRURL):
            parse_pr_url("https://github.com/owner/repo/issues/42")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    """_paginate must follow Link headers across multiple pages."""

    def test_two_page_files_list(self) -> None:
        """Files spanning two pages must be concatenated into one list.

        Tests that build_pr_context uses ALL paginated results.  We mock
        get_pull_request_files to return two items (simulating two pages
        merged by _paginate) and verify both appear in the context.
        """
        page1 = [{"filename": "a.py", "status": "modified", "additions": 1,
                   "deletions": 0, "changes": 1}]
        page2 = [{"filename": "b.py", "status": "added", "additions": 5,
                   "deletions": 0, "changes": 5}]
        all_files = page1 + page2  # What _paginate would produce after merging pages.

        with (
            patch.object(GitHubClient, "get_pull_request", return_value={**FAKE_PR, "changed_files": 2}),
            patch.object(GitHubClient, "get_pull_request_files", return_value=all_files),
            patch.object(GitHubClient, "get_pull_request_commits", return_value=FAKE_COMMITS),
            patch.object(GitHubClient, "get_file_contents", side_effect=GitHubHTTPError(404, "Not Found")),
        ):
            ctx = build_pr_context(make_client(), OWNER, REPO, PR_NUM, fetch_source=True)

        assert len(ctx.files) == 2
        paths = {f.path for f in ctx.files}
        assert "a.py" in paths
        assert "b.py" in paths

    def test_paginate_link_header_followed(self) -> None:
        """_paginate itself must follow Link headers for next pages.

        Tests the _paginate method in isolation using exact query-param URL
        matching so respx correctly distinguishes page1 and page2.
        """
        files_path = f"/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files"
        page1_url = f"{BASE_URL}{files_path}?per_page=100"
        page2_url = f"{BASE_URL}{files_path}?page=2&per_page=100"

        page1_data = [{"filename": "a.py"}]
        page2_data = [{"filename": "b.py"}]

        with respx.mock:
            # Match page1 with exact query params (per_page=100, no page param)
            respx.get(page1_url).mock(
                return_value=httpx.Response(
                    200,
                    json=page1_data,
                    headers={"link": f'<{page2_url}>; rel="next"'},
                )
            )
            # Match page2 with exact query params
            respx.get(page2_url).mock(
                return_value=httpx.Response(200, json=page2_data)
            )

            client = GitHubClient(token=FAKE_TOKEN)
            result = client._paginate(files_path)

        assert len(result) == 2
        assert result[0]["filename"] == "a.py"
        assert result[1]["filename"] == "b.py"

    def test_parse_next_link_present(self) -> None:
        header = '<https://api.github.com/repos/o/r/pulls/1/files?page=2>; rel="next", <...>; rel="last"'
        result = _parse_next_link(header)
        assert result == "https://api.github.com/repos/o/r/pulls/1/files?page=2"

    def test_parse_next_link_absent(self) -> None:
        header = '<https://api.github.com/repos/o/r/pulls/1/files?page=1>; rel="first"'
        result = _parse_next_link(header)
        assert result is None

    def test_parse_next_link_empty(self) -> None:
        assert _parse_next_link("") is None

    def test_parse_next_link_last_page(self) -> None:
        header = '<https://api.github.com/repos/o/r/pulls/1/files?page=3>; rel="last"'
        result = _parse_next_link(header)
        assert result is None

    def test_abs_to_relative_strips_base(self) -> None:
        result = _abs_to_relative(
            "https://api.github.com/repos/o/r/pulls/1/files?page=2",
            "https://api.github.com",
        )
        assert result == "/repos/o/r/pulls/1/files?page=2"

    def test_abs_to_relative_unknown_base_unchanged(self) -> None:
        result = _abs_to_relative(
            "https://other.example.com/path",
            "https://api.github.com",
        )
        assert result == "https://other.example.com/path"


# ---------------------------------------------------------------------------
# Rubric injection tests
# ---------------------------------------------------------------------------

from server import review_pull_request as _tool_review_pull_request
from review.prompts import REVIEW_RUBRIC


class TestRubricInjection:
    """review_pull_request must embed review_instructions (the rubric) in its response."""

    PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{PR_NUM}"

    @respx.mock
    def test_review_instructions_key_is_present(self) -> None:
        _mock_standard_pr()
        result = _tool_review_pull_request(self.PR_URL)
        assert "review_instructions" in result, (
            "review_pull_request must include a 'review_instructions' key "
            "so Claude receives the rubric via the tool-calling path."
        )

    @respx.mock
    def test_review_instructions_is_full_rubric(self) -> None:
        _mock_standard_pr()
        result = _tool_review_pull_request(self.PR_URL)
        assert result["review_instructions"] == REVIEW_RUBRIC

    @respx.mock
    def test_review_instructions_contains_key_categories(self) -> None:
        _mock_standard_pr()
        result = _tool_review_pull_request(self.PR_URL)
        rubric = result["review_instructions"]
        for category in ["CORRECTNESS", "PERFORMANCE", "SECURITY", "CONCURRENCY", "TESTING"]:
            assert category in rubric, f"Rubric missing category: {category}"
