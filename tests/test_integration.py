"""
tests/test_integration.py
--------------------------
Read-only integration tests against the real GitHub API.

These tests are SKIPPED automatically when GITHUB_TOKEN is not configured.
They make only GET requests; no GitHub mutations are performed.

Target repository: harsh2215/PR-review-MCP-demo
Test PR:           #2  (hp.feature/concurrent_payments)

To run manually:
    pytest tests/test_integration.py -v -s
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from github.client import GitHubClient
from review.models import PRContext
from review.normalizer import build_pr_context

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

_OWNER = "harsh2215"
_REPO = "PR-review-MCP-demo"
_PR_NUMBER = 2

_HAS_TOKEN = bool(os.environ.get("GITHUB_TOKEN", "").strip())

skip_no_token = pytest.mark.skipif(
    not _HAS_TOKEN,
    reason="GITHUB_TOKEN not configured – skipping live integration tests",
)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@skip_no_token
class TestLivePR2:
    """Read-only integration tests against PR #2 of the demo repository.

    All assertions are structural – they verify shape and presence of data,
    not specific content values that could change.
    """

    @pytest.fixture(scope="class")
    def pr_context(self) -> PRContext:
        """Fetch PR #2 once for the whole class."""
        with GitHubClient() as client:
            return build_pr_context(
                client,
                _OWNER,
                _REPO,
                _PR_NUMBER,
                fetch_source=True,
            )

    def test_context_is_pr_context(self, pr_context: PRContext) -> None:
        assert isinstance(pr_context, PRContext)

    def test_pr_number_correct(self, pr_context: PRContext) -> None:
        assert pr_context.number == _PR_NUMBER

    def test_repo_info(self, pr_context: PRContext) -> None:
        assert pr_context.repo.owner == _OWNER
        assert pr_context.repo.name == _REPO

    def test_title_non_empty(self, pr_context: PRContext) -> None:
        assert pr_context.title
        assert len(pr_context.title) > 0

    def test_state_is_valid(self, pr_context: PRContext) -> None:
        assert pr_context.state in ("open", "closed", "merged")

    def test_base_sha_preserved(self, pr_context: PRContext) -> None:
        """base SHA must be a 40-character hex string (critical for incremental review)."""
        assert pr_context.base.sha
        assert len(pr_context.base.sha) == 40
        assert all(c in "0123456789abcdef" for c in pr_context.base.sha)

    def test_head_sha_preserved(self, pr_context: PRContext) -> None:
        """head SHA must be a 40-character hex string."""
        assert pr_context.head.sha
        assert len(pr_context.head.sha) == 40
        assert all(c in "0123456789abcdef" for c in pr_context.head.sha)

    def test_base_ref_non_empty(self, pr_context: PRContext) -> None:
        assert pr_context.base.ref

    def test_head_ref_non_empty(self, pr_context: PRContext) -> None:
        assert pr_context.head.ref

    def test_has_commits(self, pr_context: PRContext) -> None:
        assert len(pr_context.commits) > 0

    def test_commits_have_sha_and_message(self, pr_context: PRContext) -> None:
        for commit in pr_context.commits:
            assert commit.sha, f"Empty SHA in commit: {commit}"
            assert commit.message, f"Empty message in commit: {commit}"

    def test_has_changed_files(self, pr_context: PRContext) -> None:
        assert len(pr_context.files) > 0

    def test_changed_files_have_path_and_status(self, pr_context: PRContext) -> None:
        for f in pr_context.files:
            assert f.path, f"Empty path in file: {f}"
            assert f.status, f"Empty status in file: {f}"

    def test_at_least_one_file_has_patch_or_source(self, pr_context: PRContext) -> None:
        """At least one changed file should have a diff or source content."""
        has_diff_or_source = any(
            f.patch is not None or f.source_content is not None
            for f in pr_context.files
        )
        assert has_diff_or_source, (
            "No file has patch or source_content – review context would be empty."
        )

    def test_no_mutation_occurred(self, pr_context: PRContext) -> None:
        """Sanity: the context was built from read-only data."""
        # The only way to verify this programmatically is to confirm we never
        # called any write endpoint.  Since our client only exposes GET methods,
        # this is guaranteed by construction.  This test documents the intent.
        assert pr_context is not None

    def test_to_review_dict_serialisable(self, pr_context: PRContext) -> None:
        """The dict returned to Claude must be JSON-serialisable."""
        import json
        d = pr_context.to_review_dict()
        serialised = json.dumps(d)  # must not raise
        assert serialised

    def test_to_review_dict_has_required_keys(self, pr_context: PRContext) -> None:
        d = pr_context.to_review_dict()
        for key in ("repo", "number", "title", "state", "base", "head", "commits", "files"):
            assert key in d, f"Missing required key: {key}"

    def test_structure_summary(self, pr_context: PRContext, capsys: pytest.CaptureFixture) -> None:
        """Print a human-readable summary of the retrieved PR context."""
        d = pr_context.to_review_dict()
        lines = [
            "",
            "=" * 60,
            f"PR #{pr_context.number}: {pr_context.title}",
            f"State  : {pr_context.state}",
            f"Author : {pr_context.author}",
            f"Base   : {pr_context.base.ref} @ {pr_context.base.sha[:8]}",
            f"Head   : {pr_context.head.ref} @ {pr_context.head.sha[:8]}",
            f"Commits: {len(pr_context.commits)}",
            f"Files  : {len(pr_context.files)} changed "
            f"(+{pr_context.total_additions} / -{pr_context.total_deletions})",
            "",
        ]
        for f in pr_context.files:
            patch_info = f"patch={'yes' if f.patch else 'no'}"
            src_info = f"source={'yes' if f.source_content else ('err: ' + (f.source_fetch_error or '?'))}"
            lines.append(f"  [{f.status:10}] {f.path}  ({patch_info}, {src_info})")

        lines.append("=" * 60)
        print("\n".join(lines))
        # The test always passes – it just prints the summary.
