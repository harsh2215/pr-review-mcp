# PR Review MCP

A **Model Context Protocol (MCP) server** that gives Claude Desktop the ability
to retrieve GitHub pull requests and submit structured code reviews.

**Claude does the AI reasoning.  The MCP server does the GitHub plumbing.**

---

## Architecture

```
Claude Desktop
      │
      │  review_pull_request(PR URL)
      ▼
 MCP Server  ──► GitHub REST API
      │
      │  PRContext (normalized PR data)
      ▼
 Claude semantic reasoning
      │
      │  ReviewResult (structured findings)
      ▼
 MCP Server
      │
      ├── dry_run=True  ──► validation + preview (read-only)
      │
      └── dry_run=False ──► GitHub review (one atomic POST)
```

### Responsibilities

| Layer | Who | What |
|-------|-----|------|
| **GitHub access** | MCP server | Fetches PR metadata, diffs, commits, source content |
| **Data normalization** | MCP server | Maps GitHub API shapes to clean `PRContext` |
| **Review rubric** | MCP server | Exposes `review/prompts.py` to Claude |
| **Semantic review** | **Claude** | Inspects code, identifies issues, produces `ReviewResult` |
| **Validation** | MCP server | Schema-validates `ReviewResult`, checks inline line locations |
| **Payload building** | MCP server | Renders review body + inline comment API payload |
| **Submission** | MCP server | Optionally POSTs one GitHub review (`dry_run=False`) |

> **The MCP server never calls an LLM.**  It is purely a data-access and
> validation layer.  Claude performs all semantic reasoning.

---

## Project Structure

```
pr-review-mcp/
├── server.py               # MCP server — exposes 2 tools
├── run_mcp.sh              # Launcher for Claude Desktop (stdio)
├── requirements.txt        # Python dependencies
├── .env                    # GITHUB_TOKEN (gitignored)
├── .env.example            # Template (safe to commit)
│
├── github/
│   ├── __init__.py
│   └── client.py           # Authenticated GitHub REST client
│
├── review/
│   ├── __init__.py
│   ├── models.py           # PRContext + ReviewFinding/ReviewResult schemas
│   ├── normalizer.py       # GitHub API → PRContext
│   ├── prompts.py          # Review rubric for Claude
│   └── submission.py       # Diff validation + payload builder
│
├── utils/
│   ├── __init__.py
│   └── github_url.py       # GitHub PR URL parser
│
└── tests/
    ├── conftest.py
    ├── test_github_client.py
    ├── test_github_url.py
    ├── test_integration.py     # Live tests (requires GITHUB_TOKEN)
    ├── test_review_models.py
    ├── test_review_tool.py
    └── test_submit_pr_review.py
```

---

## One-Time Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/pr-review-mcp.git
cd pr-review-mcp
```

### 2. Install Python dependencies

The project uses Python 3.10+.  Install to the system or a virtual environment:

```bash
# Option A: system/Anaconda Python (simplest)
pip install -r requirements.txt

# Option B: virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`run_mcp.sh` automatically detects `.venv/bin/python`, `venv/bin/python`, or
falls back to the system `python3`.

### 3. Configure the GitHub PAT

Create a **fine-grained personal access token** on GitHub with the following
repository permissions for the repositories you want to review:

| Permission | Level | Required for |
|-----------|-------|-------------|
| Contents | Read | Fetching file source |
| Pull requests | Read | Fetching PR metadata, diffs, commits |
| Pull requests | Write | Submitting reviews (`dry_run=False`) |

Copy `.env.example` → `.env` and fill in your token:

```bash
cp .env.example .env
# Edit .env and set your token:
# GITHUB_TOKEN=ghp_your_token_here
```

> **Never commit `.env`.**  It is gitignored.  Never put the token in code,
> logs, exceptions, or Claude Desktop configuration.

### 4. Make the launcher executable

```bash
chmod +x run_mcp.sh
```

---

## Claude Desktop Local MCP Configuration

Add the MCP server to Claude Desktop's configuration file.

**Configuration file location:**

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

**Add this entry** (replace the path with your actual project location):

```json
{
  "mcpServers": {
    "pr-review": {
      "command": "/home/YOUR_USERNAME/Desktop/Lilly/pr-review-mcp/run_mcp.sh",
      "args": []
    }
  }
}
```

> The `run_mcp.sh` script loads `GITHUB_TOKEN` from `.env` automatically via
> `python-dotenv`.  You do not need to put the token in the Claude Desktop
> configuration.

Restart Claude Desktop after editing the configuration.  The tools
`review_pull_request` and `submit_pr_review` will appear in the tool list.

---

## MCP Tools

### `review_pull_request`

**Read-only.**  Retrieves and normalizes GitHub PR data.  Returns a
`PRContext` dict for Claude to reason over.  Never modifies GitHub.

```
review_pull_request(pr_url: str) → PRContext dict
```

**Input:** Full GitHub PR URL, e.g. `https://github.com/owner/repo/pull/42`

**Returns:** Normalized PR context including:
- Repository owner/name
- PR title, description, state, author
- Base and head branch refs + SHAs
- All commits with messages
- All changed files with unified diff patches
- Full source content of text files at the head SHA

---

### `submit_pr_review`

Validates a `ReviewResult` produced by Claude, classifies findings, and
optionally submits a GitHub review.

```
submit_pr_review(
    pr_url:   str,
    findings: list[dict],   # list of ReviewFinding dicts
    summary:  dict,         # ReviewSummary dict
    dry_run:  bool = True   # default: preview only, no mutation
) → result dict
```

**`dry_run=True` (default):** All validation + preview.  Zero GitHub mutations.

**`dry_run=False`:** Submits ONE atomic GitHub review (body + all inline
comments in a single API call).

**Returns:**
- `head_sha`: The freshly re-fetched current PR head SHA
- `inline_findings`: Validated inline findings with confirmed diff locations
- `summary_findings`: Summary findings (no diff location required)
- `discarded_findings`: Discarded findings + reason
- `review_body`: Full markdown review body
- `inline_comments`: Exact API payload for inline comments
- `counts`: `{inline, summary, discarded}`
- `submitted`: True only after a real submission

---

## Example Workflow

### Step 1 — Retrieve PR context

In Claude Desktop:

> Call `review_pull_request("https://github.com/owner/repo/pull/42")`

Claude receives a full `PRContext` including diffs, source, and commits.

### Step 2 — Claude performs the semantic review

Claude inspects the `PRContext` using the rubric in `review/prompts.py`,
identifies issues, and constructs a `ReviewResult`:

```json
{
  "findings": [
    {
      "finding_id": "F001",
      "category": "concurrency",
      "severity": "high",
      "confidence": 0.88,
      "file": "src/payment_service.py",
      "line": 69,
      "title": "Race condition on retry_count",
      "description": "...",
      "recommendation": "...",
      "action": "inline"
    }
  ],
  "summary": {
    "verdict": "Request changes",
    "overview": "...",
    "strengths": [...],
    "risks": [...]
  }
}
```

### Step 3 — Dry-run preview

> Call `submit_pr_review(pr_url, findings, summary, dry_run=True)`

MCP re-fetches the current diff, validates every inline finding against actual
changed lines, classifies findings (INLINE / SUMMARY / DISCARD), and returns
the complete review payload — with no GitHub mutation.

### Step 4 — Inspect and approve

Review the preview.  If satisfied:

### Step 5 — Real submission (explicit user action only)

> Call `submit_pr_review(pr_url, findings, summary, dry_run=False)`

MCP submits one GitHub review containing the body and all valid inline
comments in a single atomic API call.

---

## Inline Comment Validation

**The MCP server never trusts Claude's line numbers blindly.**

For every `action: "inline"` finding, the server:

1. Re-fetches the current PR diff from GitHub.
2. Parses the unified diff to build the exact set of commentable lines (added/`+` lines only).
3. Validates that `file` exists in the PR diff.
4. Validates that `line` corresponds to an actual changed line.
5. If validation fails → **downgrade to SUMMARY** (never discard a meaningful finding).
6. If line number was invented → **safely handled**, not blindly submitted.

Inline comments use the line-based GitHub API (`line` + `side="RIGHT"`), not
the deprecated `position`-based API.

---

## HEAD SHA Safety

`submit_pr_review` always re-fetches the PR's current head SHA immediately
before building the review payload.  The review is attached to this fresh SHA.

This prevents submitting a review against a stale diff when the PR branch has
been updated since `review_pull_request` was called.

### Current idempotency limitation

Because Phase 1 is **stateless**, exactly-once submission cannot be guaranteed
if a network error causes the GitHub POST to succeed but the MCP client never
receives the response and retries.

**This is acceptable for Phase 1.**

Future phases may implement:
- Repository + PR number + HEAD SHA → review fingerprint
- Persistent submission state
- Pre-submission duplicate detection

Do not implement these now.

---

## Running Tests

```bash
# Unit tests only (no GITHUB_TOKEN required)
pytest -q -k "not integration"

# Full suite (requires GITHUB_TOKEN in .env)
pytest -q
```

Current suite: **200 tests** across 7 test files.

Live integration tests are skipped automatically when `GITHUB_TOKEN` is absent,
so the full `pytest` command is always safe to run in CI.

---

## PR #3 as the End-to-End Test Case

The controlled test repository (`PR-review-MCP-demo`) contains PR #3:
**"Add concurrent payment processing"**.

This PR intentionally contains:

1. **Race condition** in `retry_payment` — non-atomic read-check-write on
   `retry_count` without a lock.
2. **Deadlock risk** in `transfer_retry_state` — locks acquired in argument
   order rather than canonical order, creating circular-wait potential.
3. **Missing concurrency tests** — `test_transfer_retry_state` is
   single-threaded; no thread-safety or deadlock tests exist.

> **Important:** The MCP server has **no knowledge** of these bugs.
> It cannot detect them without an LLM.  Claude discovers them by reading
> the `PRContext` diff and source content, then applies the review rubric
> from `review/prompts.py`.

The MCP server's job is only to:
- Deliver the `PRContext` accurately.
- Validate Claude's `ReviewResult` against the actual diff.
- Build and (optionally) submit the GitHub review.

---

## Security

| Property | Status |
|---------|--------|
| `.env` gitignored | ✅ |
| Token never logged | ✅ |
| Token never in exceptions | ✅ |
| Token never in MCP responses | ✅ |
| Token never in Claude Desktop config | ✅ |
| Token never hard-coded | ✅ |
| `review_pull_request` read-only | ✅ |
| GitHub mutation only via `submit_pr_review` | ✅ |
| `dry_run=True` makes zero POST/PUT/PATCH/DELETE | ✅ |
| Fake tokens in tests only | ✅ |

---

## Troubleshooting

**`mcp` package not found:**
```bash
pip install -r requirements.txt
```

**`GITHUB_TOKEN is not set`:**
- Ensure `.env` exists with `GITHUB_TOKEN=ghp_...`
- Ensure the token has Read access to the target repository's pull requests

**`GitHub API error 404`:**
- Verify the PR URL is correct and the repository is accessible with your token

**`GitHub API error 403`:**
- Your fine-grained PAT may lack required permissions
- Check: Contents (read), Pull requests (read/write)

**Claude Desktop doesn't show the MCP tools:**
- Restart Claude Desktop after editing `claude_desktop_config.json`
- Check the path in the config points to the real `run_mcp.sh` location
- Run `run_mcp.sh` manually from a terminal to see error output

**`line X in 'file.py' is not a changed/commentable line`:**
- The PR branch was updated after `review_pull_request` was called
- Re-call `review_pull_request` to get the fresh diff, then re-generate findings
- Or use `action: "summary"` for findings without a valid anchor line

---

## Future Phase Ideas

The following are documented for future implementation.  **None are implemented now.**

- **Persistent review state** — store submission history keyed by repo + PR + HEAD SHA
- **Idempotency / review fingerprints** — prevent duplicate reviews on retry
- **Incremental reviews** — re-review only changed files since last review
- **AST / call graph analysis** — detect cross-function issues without LLM
- **Static analysis integration** — mypy, ruff, semgrep pre-filter
- **Dependency analysis** — flag version bumps with known CVEs
- **API compatibility checks** — detect breaking changes across versions
- **Database migration analysis** — detect unsafe schema migrations
- **CI integration** — trigger review on push/PR webhook
- **GitHub App** — replace PAT with app installation tokens
- **Webhook-driven reviews** — auto-review on PR open/update events
- **Automatic patches** — generate and post fix suggestions as PR comments
- **Benchmarking** — flag performance regressions with baseline comparison
- **Repository-level memory** — remember past findings for cross-PR context
