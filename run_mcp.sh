#!/usr/bin/env bash
# run_mcp.sh
# ----------
# Launcher for the PR Review MCP server.
#
# Designed to be invoked directly by Claude Desktop as a stdio MCP transport.
# Uses exec so stdin/stdout/signals are forwarded correctly.
#
# Usage:
#   /absolute/path/to/pr-review-mcp/run_mcp.sh
#
# Requirements:
#   - Python 3.10+ available at a known path (see PYTHON below).
#   - All dependencies installed in the resolved Python environment.
#   - GITHUB_TOKEN present in .env (loaded by the server on startup).
#
# One-time setup (run manually, NOT run by this script):
#   pip install -r requirements.txt
#
# Security: This script does NOT contain or print the GitHub PAT.
#           The PAT is loaded from .env by the MCP server at startup.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve project directory (works regardless of $PWD at call time)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Locate Python interpreter
#
# Priority:
#   1. .venv/bin/python  (project-local virtual environment)
#   2. venv/bin/python   (alternate venv name)
#   3. System python3    (Anaconda / system install)
# ---------------------------------------------------------------------------
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/.venv/bin/python"
elif [[ -x "${SCRIPT_DIR}/venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: No Python interpreter found." >&2
    echo "       Install Python 3.10+ or create a virtual environment at:" >&2
    echo "       ${SCRIPT_DIR}/.venv/" >&2
    echo "       Then run: pip install -r ${SCRIPT_DIR}/requirements.txt" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Verify that the MCP package is available in the resolved interpreter
# ---------------------------------------------------------------------------
if ! "${PYTHON}" -c "import mcp" 2>/dev/null; then
    echo "ERROR: 'mcp' package not found in Python interpreter:" >&2
    echo "       ${PYTHON}" >&2
    echo "" >&2
    echo "       Run once to install dependencies:" >&2
    echo "       ${PYTHON} -m pip install -r ${SCRIPT_DIR}/requirements.txt" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Startup confirmation  (stderr only — stdout is reserved for MCP protocol)
# ---------------------------------------------------------------------------
echo "[pr-review-mcp] ✅ Server starting at $(date '+%Y-%m-%d %H:%M:%S')" >&2
echo "[pr-review-mcp]    Python : ${PYTHON}" >&2
echo "[pr-review-mcp]    Project: ${SCRIPT_DIR}" >&2
echo "[pr-review-mcp]    Tools  : review_pull_request, submit_pr_review" >&2

# ---------------------------------------------------------------------------
# Launch MCP server via stdio transport (exec forwards stdin/stdout/signals)
# ---------------------------------------------------------------------------
exec "${PYTHON}" "${SCRIPT_DIR}/server.py" "$@"
