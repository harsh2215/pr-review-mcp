# PR Review MCP – Progress Tracker

_Last updated: 2026-09-13_

## Status: PHASE 2B COMPLETE ✅ (Targeted Repository File Retrieval)

All 313 tests pass (including URL parsing, validation, PR normalisation, Event support, and Repository normalisation/file retrieval).

---

## Files Created / Modified

| File | Status | Purpose |
|------|--------|---------|
| `github/client.py` | **Modified** | Added `get_repository`, `get_git_tree`, `get_commit` |
| `repository/models.py` | **Created** | `RepositoryContext`, `TreeEntry`, `RepositoryFile` Pydantic models |
| `repository/normalizer.py` | **Created** | `build_repository_context()`, `build_repository_file()` |
| `server.py` | **Modified** | Added `get_repository_context(repo_url, ref)` and `get_repository_file(repo_url, path, ref)` MCP tools |
| `tests/test_repository.py` | **Created** | Unit/integration tests for repository context and file retrieval |
| `utils/github_url.py` | **Modified** | Added `parse_repo_url` and `InvalidGitHubRepoURL` |

---

## Test Results

```
pytest -v
93 passed in 3.65s

  tests/test_github_url.py     29 tests  ✅
  tests/test_github_client.py  21 tests  ✅
  tests/test_review_tool.py    25 tests  ✅
  tests/test_integration.py    18 tests  ✅
```

---

## MCP Tool: `review_pull_request`

**Input:** `pr_url: str` – full GitHub PR URL  
**Output:** JSON-compatible `PRContext` dict

**Flow:**
```
Claude  →  review_pull_request(pr_url)
        →  parse_pr_url()         [utils/github_url.py]
        →  build_pr_context()     [review/normalizer.py]
              → client.get_pull_request()         (single object, no pagination)
              → client.get_pull_request_files()   (paginated via _paginate)
              → client.get_pull_request_commits() (paginated via _paginate)
              → client.get_file_contents()        (per-file at head SHA, non-fatal errors)
        →  ctx.to_review_dict()   [exclude_none=True for compact output]
        →  Claude performs code review
```

---

## PRContext Schema

```python
PRContext(
    repo        = RepoInfo(owner, name),
    number      = int,
    title       = str,
    body        = str | None,
    state       = "open" | "closed" | "merged",
    author      = str | None,
    created_at  = str | None,
    updated_at  = str | None,
    merged_at   = str | None,
    base        = BranchRef(label, ref, sha),   # target branch + commit SHA
    head        = BranchRef(label, ref, sha),   # source branch + commit SHA
    commits     = [CommitInfo(sha, message, author_name, author_email, author_date)],
    total_commits = int,
    files       = [ChangedFile(
        path, previous_path, status,
        additions, deletions, changes,
        patch,              # unified diff or None (binary / too-large)
        source_content,     # decoded UTF-8 text at head SHA, or None
        source_fetch_error, # non-fatal error message, or None
    )],
    total_additions     = int,
    total_deletions     = int,
    total_changed_files = int,
)
```

---

## Live PR #2 Integration Test Output

```
PR #2: Improve payment and user handling
State  : open
Author : harsh2215
Base   : main @ 4d7ac63a
Head   : hp.feature/payment_history @ 33465404
Commits: 1
Files  : 2 changed (+18 / -8)

  [modified  ] src/services/payment_service.py  (patch=yes, source=yes)
  [modified  ] src/services/user_service.py  (patch=yes, source=yes)
```

All 18 assertions passed including:
- ✅ base SHA = 40-char hex (`4d7ac63a...`)
- ✅ head SHA = 40-char hex (`33465404...`)
- ✅ both files have unified diff patch AND full source content
- ✅ JSON-serialisable
- ✅ no GitHub mutations (read-only)

---

## Key Engineering Decisions & Discoveries

### Pagination
- `_paginate()` in `GitHubClient` follows `Link: <url>; rel="next"` headers automatically
- GitHub's next-page URLs are **absolute** (`https://api.github.com/...`)
- `_abs_to_relative(abs_url, base_url)` strips the base so the `base_url`-configured httpx client handles them correctly
- **respx discovery**: `respx.get(path_without_query)` matches URLs with *any* query params — must register mocks with exact full query-param URL to distinguish page1 vs page2

### Source content
- Fetched at `head.sha` ref for each changed (non-`removed`) file
- Capped at 100 KB; binary/large/deleted files silently record `source_fetch_error`
- A single file failure **never** aborts the full `build_pr_context()` call

### State resolution
- GitHub returns `state: "open" | "closed"` plus a `merged: bool` field
- Normalizer maps to unambiguous three-way: `"open"`, `"closed"`, or `"merged"`

### `to_review_dict(exclude_none=True)`
- Keeps Claude's context compact — absent optional fields don't appear in the output

---

## NOT Implemented (Phase 2+)

- `ReviewFinding` model and review rubric
- `submit_pr_review` / inline comment posting
- Incremental review using base vs head SHA diff
- Repository memory / re-review awareness
- Static analysis integration
- LLM calls (the MCP server never calls an LLM)
