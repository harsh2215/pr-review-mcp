"""
repository/normalizer.py
------------------------
Logic to fetch and normalize GitHub repository context.
"""

from __future__ import annotations

import base64
from typing import Any

from github.client import GitHubClient
from repository.models import RepositoryContext, RepositoryFile, TreeEntry


def build_repository_context(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str | None = None,
) -> RepositoryContext:
    """Fetch metadata and Git tree for a repository and normalize it.

    Args:
        client: Authenticated GitHubClient.
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


def build_repository_file(
    client: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    ref: str | None = None,
    max_size_bytes: int = 100 * 1024,  # 100 KB limit
) -> RepositoryFile:
    """Fetch and decode a single repository file.

    Args:
        client: Authenticated GitHubClient.
        owner: Repository owner.
        repo: Repository name.
        path: Path to the file.
        ref: Branch name, tag, or commit SHA. If None, uses default branch.
        max_size_bytes: Maximum number of bytes to return.

    Returns:
        A structured RepositoryFile containing the decoded text, or a binary flag.

    Raises:
        ValueError: If the file cannot be fetched (e.g. not found, or it's a directory).
    """
    if ref is None:
        repo_data = client.get_repository(owner, repo)
        ref = repo_data.get("default_branch", "main")

    file_data = client.get_file_contents(owner, repo, path, ref=ref)
    
    # GitHub contents API can return a list for directories.
    if isinstance(file_data, list):
        raise ValueError(f"Path '{path}' is a directory, not a file.")

    # The API includes a 'size' field and the 'content' field is base64 encoded.
    size = file_data.get("size", 0)
    raw_b64 = file_data.get("content", "")
    
    if not raw_b64:
        # Empty file
        return RepositoryFile(
            path=path,
            ref=ref,
            size=size,
            content="",
        )

    # Decode base64 (ignoring newlines which GitHub adds)
    raw_bytes = base64.b64decode(raw_b64)
    
    is_truncated = False
    if len(raw_bytes) > max_size_bytes:
        raw_bytes = raw_bytes[:max_size_bytes]
        is_truncated = True

    try:
        text_content = raw_bytes.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        text_content = None
        is_binary = True
        is_truncated = False  # Truncation doesn't apply if we don't return content

    return RepositoryFile(
        path=path,
        ref=ref,
        size=size,
        content=text_content,
        is_binary=is_binary,
        is_truncated=is_truncated,
    )

