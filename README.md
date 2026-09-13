# PR Review MCP

A local MCP server that gives **Claude Desktop** the ability to retrieve GitHub Pull Requests and submit structured code reviews.

**Claude does the AI reasoning. The MCP server does the GitHub plumbing.**

---

## Architecture

```
Claude Desktop
      │
      │  review_pull_request(PR URL)
      ▼
MCP Server  ──►  GitHub REST API  ──►  PRContext
      │
      │  submit_pr_review(findings, summary, dry_run=True)
      ▼
Validate against real diff  ──►  Submit (or dry-run)
```

- **Claude** reads the `PRContext` and produces semantic findings (`ReviewResult`).
- **The MCP server** never calls an LLM. It only fetches, normalises, validates, and (optionally) submits.

---

## Tools

| Tool | Description |
|------|-------------|
| `review_pull_request` | Fetches and normalises a GitHub PR — diffs, commits, source content. Read-only. |
| `get_repository_context` | Fetches repository metadata and the Git tree (file/directory paths) for a given branch/commit without fetching file contents. Read-only. |
| `get_repository_file` | Fetches the text content of a single repository file at a specific branch/commit. Supports text files up to 100KB. Read-only. |
| `submit_pr_review` | Validates Claude's findings against the real diff and submits a GitHub review. Use `dry_run=True` (default) to preview without mutating GitHub. |

---

## Quickstart — Install the Bundle (recommended)

The easiest way to connect this server to Claude Desktop is via the `.mcpb` bundle.

### 1. Prerequisites

Python 3.10+ and the required packages:

```bash
pip install -r requirements.txt
```

### 2. Build the bundle

```bash
npm install -g @anthropic-ai/mcpb   # one-time install of the CLI
mcpb pack                            # creates pr-review-mcp.mcpb
```

### 3. Install in Claude Desktop

Double-click `pr-review-mcp.mcpb` **or** open it with Claude Desktop.

Claude Desktop will show an installation dialog and ask for your **GitHub Token**:

- Create a fine-grained PAT at <https://github.com/settings/tokens?type=beta>
- Required permissions: **Contents** (read), **Pull requests** (read + write)

Once installed, both tools appear automatically in every Claude Desktop conversation.

---

## Quickstart — Manual Config (dev/advanced)

If you prefer to skip the bundle:

### 1. Add to `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pr-review-mcp": {
      "command": "/home/YOUR_USER/Desktop/Lilly/pr-review-mcp/run_mcp.sh"
    }
  }
}
```

### 2. Set your GitHub token

Create a `.env` file in the project directory (gitignored):

```bash
cp .env.example .env
# edit .env and set GITHUB_TOKEN=ghp_...
```

### 3. Restart Claude Desktop

Fully quit and reopen Claude Desktop.

---

## GitHub Token

| Method | Where the token goes |
|--------|----------------------|
| Bundle install | Entered once in Claude Desktop's install dialog; stored securely by the app |
| Manual config | `.env` file in the project directory (never committed) |

**Security invariants:**
- The token is never logged, included in error messages, or returned in MCP responses.
- `.env` is gitignored. The `.mcpb` bundle never contains a token.

---

## Project Structure

```
pr-review-mcp/
├── server.py              # MCP entry point — registers both tools
├── manifest.json          # MCPB bundle manifest (v0.3)
├── requirements.txt       # Python dependencies
├── run_mcp.sh             # Dev launcher (for manual claude_desktop_config.json)
├── github/
│   ├── client.py          # GitHub REST API client
│   └── __init__.py
├── review/
│   ├── models.py          # PRContext, ReviewFinding, ReviewResult
│   ├── normalizer.py      # GitHub API → PRContext
│   ├── prompts.py         # Review rubric / system prompt for Claude
│   ├── submission.py      # Diff validation + GitHub review submission
│   └── __init__.py
├── utils/
│   ├── github_url.py      # PR URL parser
│   └── __init__.py
└── tests/                 # 200 tests (unit + live integration)
```

---

## Testing a PR

### Step 1 — Ask Claude to retrieve the PR

```
Review the pull request at https://github.com/OWNER/REPO/pull/N
```

Claude will call `review_pull_request` and receive the full `PRContext`.

### Step 2 — Claude produces findings

Claude analyses the diff and produces a `ReviewResult` with typed `ReviewFinding` objects (severity, category, file, line, recommendation).

### Step 3 — Dry-run submission (safe)

```
Submit the review in dry-run mode.
```

Claude calls `submit_pr_review(..., dry_run=True)`. The server:
1. Re-fetches the current diff from GitHub.
2. Validates every finding's anchor line against the real diff.
3. Downgrades invalid anchors to `SUMMARY` (never discards valid findings).
4. Returns a full validation report — **no GitHub mutation**.

### Step 4 — Live submission (optional)

```
Submit the review for real.
```

Claude calls `submit_pr_review(..., dry_run=False)`. The server posts a single atomic GitHub review.

---

## Running Tests

```bash
pytest -q
```

Unit tests run without a token. Live integration tests (18 tests against PR #2) run only when `GITHUB_TOKEN` is set.

---

## Building the Bundle

```bash
mcpb pack
```

Output: `pr-review-mcp.mcpb` (≈35KB, 13 files, no credentials).

To verify the manifest is valid before packing:

```bash
mcpb validate   # or: mcpb pack (always validates first)
```

---

## Dry-run Behaviour

| Scenario | Result |
|----------|--------|
| `dry_run=True` | Zero GitHub mutations. Returns full classification report. |
| Finding line in diff | Classified `INLINE` → becomes inline comment on submit |
| Finding line not in diff | Downgraded to `SUMMARY` (safe, not discarded) |
| Confidence < 0.50 | `DISCARD` — excluded from both inline and summary |
| `dry_run=False` | Posts one atomic `POST /pulls/{n}/reviews` to GitHub |
