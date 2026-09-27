# PR Review MCP

A Model Context Protocol (MCP) server that gives **Claude AI and any other MCP-compatible AI agent** the ability to retrieve GitHub Pull Requests, explore repository structures, and submit structured code reviews.

**LLM does the AI reasoning. The MCP server does the GitHub plumbing.**

---

## Architecture

The server supports **two deployment modes** that share the exact same four MCP tools:

```
┌─────────────────────────────────────────────────────────────────┐
│                        LOCAL (HTTP)                             │
│                                                                 │
│   Any MCP Client (Claude / IDE / Inspector)                     │
│         │  stdio/JSON-RPC or HTTP                               │
│         ▼                                                       │
│   server.py  ──►  github/client.py  ──►  GitHub REST API        │
│         │         (PAT via .env)                                │
│         ▼                                                       │
│   PRContext / ReviewResult returned                             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    CLOUD (HTTP + OAuth)                         │
│                                                                 │
│   Any MCP Client (IDE / mcp-remote / Inspector)                 │
│         │  HTTP SSE  ─►  pr-review-mcp.onrender.com/mcp         │
│         ▼                                                       │
│   FastMCP (GitHubProvider OAuth)                                │
│         │  Bearer token (user's GitHub OAuth session)           │
│         ▼                                                       │
│   server.py  ──►  github/client.py  ──►  GitHub REST API        │
│         ▼                                                       │
│   PRContext / ReviewResult returned to the client               │
└─────────────────────────────────────────────────────────────────┘
```

**Key design principle:** Claude never calls an LLM. The server only fetches, normalises, validates and (optionally) submits.

---

## Tools

| Tool | Description |
|------|-------------|
| `review_pull_request` | Fetches and normalises a GitHub PR — diffs, commits, source content. Read-only. Returns a full engineering rubric for Claude to evaluate against. |
| `get_repository_context` | Fetches repository metadata and the complete Git tree (file/directory paths) for a given branch/commit. Read-only. |
| `get_repository_file` | Fetches the text content of a single repository file at a specific branch/commit. Supports text files up to 100 KB. Read-only. |
| `submit_pr_review` | Validates Claude's findings against the real diff and submits a GitHub review. Defaults to `dry_run=True` — no mutation without explicit opt-in. |

---

## Installation

### Option A — Cloud server (Recommended)

The server is deployed at `https://pr-review-mcp.onrender.com`. Connect any MCP client with a single URL — no Python install, no PAT stored in a file.

**Authentication:** The cloud server uses **GitHub OAuth**. On first connection, your browser opens a GitHub authorisation page. The token is scoped to your own GitHub account and handled entirely by the server.

#### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "pr-review": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://pr-review-mcp.onrender.com/mcp"
      ]
    }
  }
}
```

> **Note:** `mcp-remote` handles the OAuth handshake and token refresh automatically.

#### Antigravity IDE / VS Code (`~/.gemini/config/mcp_config.json`)

```json
{
  "mcpServers": {
    "pr-review": {
      "type": "http",
      "url": "https://pr-review-mcp.onrender.com/mcp"
    }
  }
}
```

---

### Option B — Run locally over HTTP

Run the server on your own machine and point your MCP client at `localhost`. Useful for development or offline use.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your token
echo "GITHUB_TOKEN=ghp_your_token" > .env

# 3. Start the server
python3 server.py --transport http --host 127.0.0.1 --port 8000
```

Then configure your MCP client:

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pr-review": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://127.0.0.1:8000/mcp"
      ]
    }
  }
}
```

**Antigravity IDE / VS Code** (`mcp_config.json`):

```json
{
  "mcpServers": {
    "pr-review": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

> **Tip:** Comment out `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in your `.env` to disable OAuth for local connections — the server falls back to the `GITHUB_TOKEN` PAT automatically.

---

## Usage

### Step 1 — Ask Claude to retrieve a PR

```
Review the pull request at https://github.com/OWNER/REPO/pull/N
```

Claude calls `review_pull_request` and receives the full `PRContext` plus the embedded engineering rubric.

### Step 2 — Claude produces findings

Claude analyses the diff and produces a `ReviewResult` with typed `ReviewFinding` objects (severity, category, file, line, recommendation, confidence).

### Step 3 — Dry-run submission (safe preview)

```
Submit the review in dry-run mode.
```

Claude calls `submit_pr_review(..., dry_run=True)`. The server:
1. Re-fetches the current diff from GitHub.
2. Validates every finding's anchor line against the real diff.
3. Downgrades invalid anchors to `SUMMARY` (never silently discards valid findings).
4. Returns a full validation report — **zero GitHub mutation**.

### Step 4 — Live submission (optional)

```
Submit the review for real.
```

Claude calls `submit_pr_review(..., dry_run=False)`. The server posts one atomic GitHub review.

---

## GitHub Token / Auth

| Mode | Auth method | Where token comes from |
|------|-------------|------------------------|
| Cloud (Render) | GitHub OAuth | Browser login — no token stored locally |
| Local HTTP | Fine-grained PAT | `.env` file (gitignored) |

**Security invariants:**
- The token is never logged, included in error messages, or returned in MCP responses.
- The `.mcpb` bundle never contains a token.
- `dry_run=True` is the default — the server never mutates GitHub without an explicit opt-in.

---

## Project Structure

```
pr-review-mcp/
├── server.py              # MCP entry point — registers all four tools
├── manifest.json          # MCPB bundle manifest (v0.3)
├── requirements.txt       # Python dependencies
├── github/
│   ├── client.py          # GitHub REST API client (sync, httpx)
│   └── __init__.py
├── review/
│   ├── models.py          # PRContext, ReviewFinding, ReviewResult (Pydantic)
│   ├── normalizer.py      # GitHub API → PRContext
│   ├── prompts.py         # Engineering rubric injected into tool response
│   ├── submission.py      # Diff validation + GitHub review submission
│   └── __init__.py
├── repository/
│   ├── models.py          # RepositoryContext, RepositoryFile (Pydantic)
│   ├── normalizer.py      # GitHub API → RepositoryContext / RepositoryFile
│   └── __init__.py
├── utils/
│   ├── github_url.py      # PR and repo URL parser
│   └── __init__.py
└── tests/                 # 310 tests (unit + live integration)
```

---

## Running Tests

```bash
pytest -q
```

Unit tests run without a token. Live integration tests run only when `GITHUB_TOKEN` is set in the environment.

---

## Building the Bundle

```bash
mcpb pack
```

Output: `pr-review-mcp.mcpb` — a single portable archive. No credentials included.

```bash
mcpb validate   # validates the manifest before packing (also runs automatically on pack)
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
