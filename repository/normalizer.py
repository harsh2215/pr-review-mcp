"""
repository/normalizer.py
------------------------
Logic to fetch and normalize GitHub repository context.
"""

from __future__ import annotations

from typing import Any

from github.client import GitHubClient
from repository.models import RepositoryContext, TreeEntry


def build_repository_context(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str | None = None,
) -> RepositoryContext:
    """Fetch metadata and Git tree for a repository and normalize it.

    Args:
        client: Au
        tests/test_repository.pythenticated GitHubClient.
        owner: Repository owner.
        repo: Repository name.
        ref: Branch name, tag, or commit SHA. If None, uses the repository's
             default branch.

    Returns:
        A concise structured RepositoryContext.

    Raises:
        ValueError: If the repository or ref cannot be found or accessed.
    """
    # 1. Fetch repository metadata
    repo_data = client.get_repository(owner, repo)
    default_branch = repo_data.get("default_branch", "main")
    
    # Determine the target ref
    target_ref = ref if ref else default_branch

    # 2. Fetch the commit to get the tree SHA
    commit_data = client.get_commit(owner, repo, target_ref)
    commit_sha = commit_data.get("sha", "")
    tree_sha = commit_data.get("commit", {}).get("tree", {}).get("sha", "")
    
    if not tree_sha:
        raise ValueError(f"Could not determine tree SHA for ref {target_ref}")

    # 3. Fetch the Git tree (recursive)
    tree_data = client.get_git_tree(owner, repo, tree_sha, recursive=True)
    tree_truncated = tree_data.get("truncated", False)
    
    # 4. Process tree entries
    tree_entries = []
    for entry in tree_data.get("tree", []):
        entry_type = entry.get("type")
        # GitHub type "blob" -> file, "tree" -> directory
        if entry_type in ("blob", "tree"):
            tree_entries.append(TreeEntry(
                path=entry.get("path", ""),
                type=entry_type,
            ))

    # 5. Build and return the structured context
    return RepositoryContext(
        owner=owner,
        repo=repo,
        full_name=repo_data.get("full_name", f"{owner}/{repo}"),
        description=repo_data.get("description"),
        default_branch=default_branch,
        private=repo_data.get("private", False),
        ref=target_ref,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        tree_truncated=tree_truncated,
        tree=tree_entries,
    )

