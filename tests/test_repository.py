"""
tests/test_repository.py
------------------------
Unit and integration tests for repository context fetching (Phase 2A).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from github.client import GitHubClient, GitHubHTTPError
from repository.models import RepositoryContext, TreeEntry
from repository.normalizer import build_repository_context, build_repository_file
from server import get_repository_context, get_repository_file
import base64

# ---------------------------------------------------------------------------
# Test Data
# ---------------------------------------------------------------------------

OWNER = "harsh2215"
REPO = "PR-review-MCP-demo"
BASE_URL = "https://api.github.com"
REPO_URL = f"https://github.com/{OWNER}/{REPO}"

MOCK_REPO_DATA = {
    "default_branch": "main",
    "description": "Demo repository",
    "full_name": f"{OWNER}/{REPO}",
    "private": False,
}

MOCK_COMMIT_DATA = {
    "sha": "commit12345",
    "commit": {
        "tree": {
            "sha": "tree12345"
        }
    }
}

MOCK_TREE_DATA = {
    "sha": "tree12345",
    "truncated": False,
    "tree": [
        {"path": "README.md", "type": "blob", "size": 1024},
        {"path": "src", "type": "tree"},
        {"path": "src/main.py", "type": "blob", "size": 512},
        {"path": "ignored_submodule", "type": "commit"} # Should be ignored
    ]
}

def _mock_file_data(content_str: str) -> dict[str, Any]:
    b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    return {
        "size": len(content_str),
        "content": b64,
        "encoding": "base64",
    }

def _mock_github_repository_reads(
    ref: str = "main",
    repo_data: dict[str, Any] | None = None,
    commit_data: dict[str, Any] | None = None,
    tree_data: dict[str, Any] | None = None,
) -> None:
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json=repo_data or MOCK_REPO_DATA)
    )
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/commits/{ref}").mock(
        return_value=httpx.Response(200, json=commit_data or MOCK_COMMIT_DATA)
    )
    commit_info = (commit_data or MOCK_COMMIT_DATA).get("commit", {})
    tree_sha = commit_info.get("tree", {}).get("sha", "dummy_tree_sha")
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/git/trees/{tree_sha}?recursive=1").mock(
        return_value=httpx.Response(200, json=tree_data or MOCK_TREE_DATA)
    )


# ---------------------------------------------------------------------------
# Normalizer Tests
# ---------------------------------------------------------------------------

class TestRepositoryNormalizer:
    def test_build_repository_context_default_branch(self) -> None:
        with respx.mock:
            _mock_github_repository_reads(ref="main")
            with GitHubClient() as client:
                ctx = build_repository_context(client, OWNER, REPO)
            
            assert ctx.owner == OWNER
            assert ctx.repo == REPO
            assert ctx.default_branch == "main"
            assert ctx.ref == "main"
            assert ctx.commit_sha == "commit12345"
            assert ctx.tree_sha == "tree12345"
            assert not ctx.tree_truncated
            
            # Tree parsing (ignoring non blob/tree types)
            assert len(ctx.tree) == 3
            paths = [e.path for e in ctx.tree]
            assert "README.md" in paths
            assert "src" in paths
            assert "src/main.py" in paths
            assert "ignored_submodule" not in paths

    def test_build_repository_context_explicit_ref(self) -> None:
        with respx.mock:
            _mock_github_repository_reads(ref="feature-branch")
            with GitHubClient() as client:
                ctx = build_repository_context(client, OWNER, REPO, ref="feature-branch")
            assert ctx.ref == "feature-branch"

    def test_build_repository_context_handles_truncated_tree(self) -> None:
        truncated_tree_data = {**MOCK_TREE_DATA, "truncated": True}
        with respx.mock:
            _mock_github_repository_reads(tree_data=truncated_tree_data)
            with GitHubClient() as client:
                ctx = build_repository_context(client, OWNER, REPO)
            assert ctx.tree_truncated is True

    def test_missing_tree_sha_raises_error(self) -> None:
        bad_commit_data = {"sha": "commit12345", "commit": {}}
        with respx.mock:
            _mock_github_repository_reads(commit_data=bad_commit_data)
            with GitHubClient() as client:
                with pytest.raises(ValueError, match="Could not determine tree SHA"):
                    build_repository_context(client, OWNER, REPO)


class TestRepositoryFileNormalizer:
    def test_build_repository_file_default_branch(self) -> None:
        with respx.mock:
            _mock_github_repository_reads(ref="main")
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/README.md?ref=main").mock(
                return_value=httpx.Response(200, json=_mock_file_data("Hello World"))
            )
            with GitHubClient() as client:
                f = build_repository_file(client, OWNER, REPO, "README.md")
            
            assert f.path == "README.md"
            assert f.ref == "main"
            assert f.content == "Hello World"
            assert f.size == 11
            assert not f.is_binary
            assert not f.is_truncated

    def test_build_repository_file_explicit_ref(self) -> None:
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/README.md?ref=v1.0").mock(
                return_value=httpx.Response(200, json=_mock_file_data("Hello v1.0"))
            )
            with GitHubClient() as client:
                f = build_repository_file(client, OWNER, REPO, "README.md", ref="v1.0")
            assert f.ref == "v1.0"
            assert f.content == "Hello v1.0"

    def test_build_repository_file_directory_raises_error(self) -> None:
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/src?ref=main").mock(
                return_value=httpx.Response(200, json=[{"type": "file"}, {"type": "dir"}])
            )
            with GitHubClient() as client:
                with pytest.raises(ValueError, match="is a directory"):
                    build_repository_file(client, OWNER, REPO, "src", ref="main")

    def test_build_repository_file_binary(self) -> None:
        binary_data = base64.b64encode(b"\xff\xfe\x00\x01").decode("utf-8")
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/image.png?ref=main").mock(
                return_value=httpx.Response(200, json={"size": 4, "content": binary_data, "encoding": "base64"})
            )
            with GitHubClient() as client:
                f = build_repository_file(client, OWNER, REPO, "image.png", ref="main")
            assert f.is_binary is True
            assert f.content is None

    def test_build_repository_file_truncation(self) -> None:
        long_content = "A" * 100
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/big.txt?ref=main").mock(
                return_value=httpx.Response(200, json=_mock_file_data(long_content))
            )
            with GitHubClient() as client:
                f = build_repository_file(client, OWNER, REPO, "big.txt", ref="main", max_size_bytes=50)
            assert f.is_truncated is True
            assert len(f.content) == 50  # type: ignore


# ---------------------------------------------------------------------------
# MCP Tool Tests
# ---------------------------------------------------------------------------

class TestGetRepositoryContextTool:
    def test_valid_repo_url_no_ref(self) -> None:
        with respx.mock:
            _mock_github_repository_reads(ref="main")
            result = get_repository_context(REPO_URL)
        
        assert result["owner"] == OWNER
        assert result["repo"] == REPO
        assert result["default_branch"] == "main"
        assert result["ref"] == "main"
        assert len(result["tree"]) == 3
        assert result["tree"][0] == {"path": "README.md", "type": "blob"}

    def test_valid_repo_url_with_ref(self) -> None:
        with respx.mock:
            _mock_github_repository_reads(ref="v1.0")
            result = get_repository_context(REPO_URL, ref="v1.0")
        assert result["ref"] == "v1.0"

    def test_invalid_repo_url(self) -> None:
        with respx.mock:
            with pytest.raises(ValueError, match="Invalid GitHub repository URL"):
                get_repository_context("https://github.com/owner/repo/pull/1")

    def test_api_error_propagated(self) -> None:
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            with pytest.raises(RuntimeError, match="Not Found"):
                get_repository_context(REPO_URL)

class TestGetRepositoryFileTool:
    def test_valid_file_url(self) -> None:
        with respx.mock:
            respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/contents/app.py?ref=dev").mock(
                return_value=httpx.Response(200, json=_mock_file_data("print(1)"))
            )
            result = get_repository_file(REPO_URL, "app.py", ref="dev")
        
        assert result["path"] == "app.py"
        assert result["ref"] == "dev"
        assert result["content"] == "print(1)"
        assert result["is_binary"] is False
        assert result["is_truncated"] is False

    def test_invalid_repo_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub repository URL"):
            get_repository_file("https://github.com/owner/repo/pull/1", "app.py")

