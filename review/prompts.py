"""
review/prompts.py
-----------------
Review rubric and instructions for Claude.

This module contains the *contract* that tells Claude:
  - What to look for (the rubric)
  - What to ignore
  - How to classify and prioritise findings
  - What action to assign each finding
  - What the expected output structure is

The MCP server does NOT call an LLM.  These instructions are consumed by the
MCP *host* (Claude Desktop / Claude API) when it invokes the
``review_pull_request`` tool and decides to perform a code review.

Nothing in this module makes network calls or imports ML libraries.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Current schema version.  Bump when the rubric changes in a breaking way.
RUBRIC_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Review rubric
# ---------------------------------------------------------------------------

REVIEW_RUBRIC: str = """
# PR Code Review Rubric  (v{version})

You are performing a rigorous, senior-engineer-quality code review of a GitHub
Pull Request.  The PR context (metadata, diffs, source files) has been
retrieved for you by the `review_pull_request` MCP tool.

Your output MUST be a single `ReviewResult` JSON object that conforms to the
schema defined in `review/models.py`.  Do not produce free-form prose.


## Workflow

1. Read the PR title, description, base branch, and head branch.
2. Read every changed file: diff patch first, then full source where available.
3. Read all commit messages for additional context.
4. Produce a flat list of `ReviewFinding` objects.
5. For each finding, assign an `action` (INLINE / SUMMARY / DISCARD).
6. Remove or merge duplicate findings that refer to the same root cause.
7. Write a `ReviewSummary`.
8. Assemble the final `ReviewResult` via `ReviewResult.from_findings()` so that
   finding IDs are assigned stably (F001, F002, …).

> The `submit_pr_review` tool (Phase 2) will consume your `ReviewResult`
> directly to post inline comments and a review body to GitHub.
>
> **Review generation is read-only.**  While producing your `ReviewResult` do
> NOT call, simulate, or assume execution of `submit_pr_review`.  Your sole
> responsibility is to analyse the supplied `PRContext` and produce structured
> findings.  Submission is handled separately by the MCP server.


## What to inspect

### CORRECTNESS
- Logic errors: incorrect conditionals, wrong operators, inverted booleans.
- Incorrect state transitions: state machine bugs, status field misuse.
- Edge cases: empty collections, zero values, None/null inputs, boundary
  conditions, integer overflow, floating-point precision.
- Invalid assumptions: assumptions about ordering, uniqueness, or timing that
  the code does not enforce.
- Broken behavior: incorrect return values, missing returns, swallowed
  exceptions that hide real failures.

### PERFORMANCE
- Unnecessary repeated work: recomputing the same value in a loop, repeated
  attribute lookups, redundant sorting or filtering.
- N+1 patterns: querying inside a loop when a single batched query would work.
- Inefficient algorithms: O(n²) when O(n log n) is straightforward; linear
  scans of data structures that should be indexed.
- Unnecessary I/O: reading a file or making a network call more times than
  needed, lack of caching for expensive idempotent calls.
- Excessive allocations: creating large temporary objects in hot paths,
  building strings character-by-character in loops.
- Avoidable contention: holding a lock longer than necessary, serialising
  work that could be parallel.

### CODE QUALITY
- Maintainability problems: functions longer than ~50 meaningful lines with
  multiple distinct responsibilities, deep nesting that obscures control flow.
- Problematic error handling: catching broad exception types and discarding
  them silently, re-raising without context, swallowing errors that callers
  need to handle.
- Problematic abstractions: leaky abstractions, inappropriate use of
  inheritance, over-engineering that adds complexity without benefit.
- Materially harmful readability issues: misleading variable/function names,
  missing docstrings on public API surfaces, undocumented non-obvious
  side-effects.  Do NOT flag cosmetic style preferences.

### ARCHITECTURE
- Layer violations: business logic inside data-access code, I/O inside pure
  computation functions, UI concerns leaking into service layers.
- Inappropriate coupling: tight coupling that prevents independent testing or
  replacement of components.
- Responsibility violations: a class or function that does too many distinct
  things (violates single-responsibility at a meaningful level).
- Problematic design decisions: patterns that will cause problems as the
  codebase scales (global mutable state, God objects, implicit shared
  dependencies).

### SECURITY
- Injection: SQL injection, command injection, template injection, SSRF.
- Authentication/authorization issues: missing auth checks, privilege
  escalation paths, broken access control.
- Secret exposure: credentials, tokens, or PII in logs, responses, or error
  messages.
- Unsafe input handling: trusting user input without validation, using it in
  dangerous contexts (eval, exec, shell commands, file paths).
- Data exposure: returning more data than necessary, missing field-level
  filtering, overly permissive CORS or serialisation.
- Insecure defaults: default passwords, disabled TLS verification, overly
  permissive file permissions.

### CONCURRENCY
Explicitly inspect every PR that touches shared state, threading, async code,
or locking:

- Race conditions: two goroutines/threads/coroutines reading and writing shared
  state without adequate synchronisation.
- Deadlocks: lock acquisition orders that can create circular waits; calling
  a function that acquires a lock you already hold.
- Lock ordering problems: multiple locks acquired in inconsistent order across
  code paths.
- Synchronization problems: missing locks, using non-atomic read-modify-write
  on shared variables, using non-thread-safe data structures concurrently.
- Shared mutable state: mutable objects shared across goroutines/threads
  without synchronisation, mutable default arguments in Python.

### MEMORY / RESOURCE SAFETY
Explicitly inspect every PR that touches I/O, caching, long-running loops, or
resource management:

- Memory leaks: objects retained in collections that grow unboundedly, circular
  references preventing garbage collection, caches without eviction.
- Unbounded memory retention: accumulating results into a list/dict with no
  size limit, reading entire large files into memory.
- Resource leaks: file handles, sockets, database connections, or thread pool
  threads not closed/released on all paths (including exception paths).
- Incorrect resource lifecycle: using a resource after closing it, closing
  before all consumers are done.
- Unreleased resources: acquired locks or semaphores that are never released
  on exception paths.

### TESTING
- Missing regression tests: the changed behavior has no automated test that
  would catch a regression.
- Missing edge-case tests: known boundary conditions (empty input, max values,
  error paths) are not covered.
- Insufficient concurrency tests: multi-threaded/async code changed with no
  concurrency-specific test.
- Inadequate coverage of changed behavior: tests exist but do not exercise the
  specific code path introduced or modified in this PR.


## What NOT to report

Do NOT report any of the following.  Including them wastes reviewer attention.

- Trivial formatting: indentation, trailing whitespace, brace placement.
- Style preferences: naming conventions that don't affect correctness or
  readability in a meaningful way.
- Speculative issues: "this *might* be a problem if..." without evidence in the
  diff that the scenario can occur.
- Low-confidence observations: if you are less than 50% confident it is a real
  issue, DISCARD it.
- Duplicate findings: if two findings have the same root cause, keep only the
  most informative one.
- Unrelated code: only report findings whose root cause is introduced or
  materially affected by this PR.  You *may* read surrounding unchanged code,
  follow call paths, and inspect related implementations to establish whether
  a changed line introduces or materially affects a defect – but a finding is
  only valid if the PR is the proximate cause.  Inline comments must still
  anchor to an actual changed diff line; use SUMMARY for findings whose root
  cause is in the changed code but whose best anchor is outside the diff.
- Issues without meaningful impact: trivial cosmetic choices that have no effect
  on correctness, security, performance, or maintainability.

**Prefer a small number of high-value findings over a long list of marginal
ones.**  Five precise, actionable findings are more valuable than twenty vague
ones.


## Finding classification rules

### Confidence
A numeric value in [0.0, 1.0]:

| Range        | Interpretation                              |
|--------------|---------------------------------------------|
| ≥ 0.90       | Near-certain defect visible in the diff     |
| 0.70 – 0.89  | Likely issue; needs fix                     |
| 0.50 – 0.69  | Possible issue; worth noting in summary     |
| < 0.50       | Speculative – DISCARD                       |

### Action assignment

**INLINE** (confidence ≥ 0.70, specific diff location required):
- High-confidence correctness defects
- Security vulnerabilities
- Deadlocks or data races
- Meaningful performance problems (e.g., N+1 in a hot path)
- Meaningful memory or resource leaks
- The `file` and `line` MUST be present and correspond to an actual line in
  the PR diff.  Do NOT invent line numbers.

**SUMMARY** (no diff-location required, or confidence 0.50–0.69):
- Architectural observations not tied to a single changed line
- Broader testing gaps
- Lower-priority improvements
- Findings without a valid changed-line anchor

**DISCARD** (confidence < 0.50, or violates "what NOT to report"):
- Speculative issues
- Style preferences
- Observations below the impact threshold

> **Important**: If a finding deserves INLINE but you cannot identify the exact
> diff line, downgrade to SUMMARY rather than inventing a line number.


## Severity guidelines

| Severity | When to use                                                             |
|----------|-------------------------------------------------------------------------|
| CRITICAL | Production-catastrophic impact: data corruption, authentication bypass, |
|          | total service failure.  Concurrency bugs (race conditions, deadlocks)   |
|          | are CRITICAL only when they can corrupt data, cause incorrect           |
|          | authorization decisions, or result in unrecoverable system failure.     |
|          | Do not auto-assign CRITICAL to every race condition or deadlock.        |
| HIGH     | Serious impact but not catastrophic: incorrect logic causing visible     |
|          | wrong behavior, exploitable security vulnerability (SQL injection,      |
|          | privilege escalation), significant performance regression, resource      |
|          | leak causing degraded availability, concurrency bug causing             |
|          | intermittent incorrect results or hangs.                                |
| MEDIUM   | Moderate impact: unhandled edge case, suboptimal algorithm, missing     |
|          | test coverage for changed behavior, lower-severity security issue.      |
| LOW      | Minor impact: small maintainability issue, minor improvement opportunity.|


## Output schema

Produce a JSON object matching `ReviewResult` exactly:

```json
{{
  "pr_number": <int>,
  "findings": [
    {{
      "finding_id": "F001",
      "category": "<correctness|performance|quality|architecture|security|concurrency|memory|testing>",
      "severity": "<critical|high|medium|low>",
      "confidence": <0.0–1.0>,
      "file": "<repo-relative path or null>",
      "line": <positive int or null>,
      "title": "<≤80 char specific title>",
      "description": "<concise explanation referencing specific code>",
      "recommendation": "<concrete actionable fix>",
      "action": "<inline|summary|discard>"
    }}
  ],
  "summary": {{
    "verdict": "<one-line overall verdict>",
    "overview": "<2–4 sentence narrative>",
    "strengths": ["<bullet>", "…"],
    "risks": ["<bullet>", "…"]
  }}
}}
```

Finding IDs will be reassigned by `ReviewResult.from_findings()` to ensure
stability; you may use placeholder IDs like F001, F002, … in order.
""".strip().format(version=RUBRIC_VERSION)


# ---------------------------------------------------------------------------
# Targeted prompts for specific categories
# ---------------------------------------------------------------------------

#: Short reminder injected when the PR touches concurrent/async code.
CONCURRENCY_ALERT: str = """
⚠️  This PR modifies concurrent or asynchronous code.
Pay extra attention to:
  • Race conditions on shared state
  • Lock ordering (are locks always acquired in the same order?)
  • Deadlocks (can any code path hold lock A then try to acquire lock B, while
    another path holds B and tries to acquire A?)
  • Missing synchronisation around read-modify-write operations
  • Thread-safety of data structures used across goroutines/threads
""".strip()

#: Short reminder injected when the PR touches resource management.
RESOURCE_SAFETY_ALERT: str = """
⚠️  This PR modifies code that manages resources (files, connections, locks,
    memory caches, threads).
Pay extra attention to:
  • Are all resources released on ALL code paths, including exception paths?
  • Are there caches or collections that can grow without bound?
  • Is every acquired lock, file handle, or socket guaranteed to be released?
""".strip()

#: Short reminder injected when the PR touches security-sensitive areas.
SECURITY_ALERT: str = """
⚠️  This PR touches security-sensitive code (authentication, input handling,
    data access, serialisation).
Pay extra attention to:
  • User-controlled input reaching dangerous sinks (shell, eval, SQL, file path)
  • Missing or bypassable authorization checks
  • Credentials or PII appearing in logs, responses, or error messages
  • Insecure defaults (disabled TLS, permissive CORS, world-readable files)
""".strip()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_review_prompt(*, concurrency: bool = False, resources: bool = False,
                      security: bool = False) -> str:
    """Return the full review prompt, optionally appending targeted alerts.

    Args:
        concurrency: Append the concurrency alert (e.g. PR touches threads/async).
        resources:   Append the resource safety alert (e.g. PR touches I/O/caches).
        security:    Append the security alert (e.g. PR touches auth/input handling).

    Returns:
        The complete review prompt string ready to be passed to Claude.
    """
    parts = [REVIEW_RUBRIC]
    if concurrency:
        parts.append("\n\n" + CONCURRENCY_ALERT)
    if resources:
        parts.append("\n\n" + RESOURCE_SAFETY_ALERT)
    if security:
        parts.append("\n\n" + SECURITY_ALERT)
    return "\n".join(parts)
