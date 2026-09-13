"""
tests/test_submit_pr_review.py
------------------------------
Unit tests for the submit_pr_review pipeline.

Covers:
- parse_changed_lines (diff parser)
- classify_findings (inline/summary/discard rules)
- build_review_payload (end-to-end payload builder)
- submit_pr_review MCP tool (dry-run and real-submission paths)

All GitHub API calls are mocked. No network access. No LLM calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from github.client import GitHubClient, GitHubHTTPError
from review.models import (
    FindingAction,
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewSummary,
)
from review.submission import (
    ClassifiedFinding,
    ReviewPayload,
    build_inline_comments,
    build_review_body,
    build_review_payload,
    classify_findings,
    parse_changed_lines,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

# Minimal but realistic unified diff with:
#   - hunk header at new line 10
#   - context line (new line 10 – not commentable)
#   - removed line (does not advance counter)
#   - added line (new line 11 – commentable)
#   - another added line (new line 12 – commentable)
#   - context line (new line 13 – not commentable)
SAMPLE_PATCH = """\
@@ -8,7 +10,8 @@
 context line
-removed line
+added line A
+added line B
 context line 2
"""

# These are the commentable lines from SAMPLE_PATCH:
# Line 10 is the first context line (not commentable).
# Line 11 is "added line A" (commentable).
# Line 12 is "added line B" (commentable).
# Line 13 is "context line 2" (not commentable).
SAMPLE_COMMENTABLE = frozenset({11, 12})

FAKE_TOKEN = "ghp_test_token_never_logged"

# A minimal ChangedFile-like object (use dict so we can pass into index builder).
class _FakeFile:
    def __init__(self, path: str, patch: str | None = None):
        self.path = path
        self.patch = patch


def make_finding(
    *,
    finding_id: str = "F001",
    category: str = "correctness",
    severity: str = "high",
    confidence: float = 0.85,
    file: str | None = "src/app.py",
    line: int | None = 11,
    title: str = "Test finding",
    description: str = "Description.",
    recommendation: str = "Fix it.",
    action: str = "inline",
) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        category=category,
        severity=severity,
        confidence=confidence,
        file=file,
        line=line,
        title=title,
        description=description,
        recommendation=recommendation,
        action=action,
    )


def make_summary() -> ReviewSummary:
    return ReviewSummary(
        verdict="Request changes",
        overview="One issue found.",
        strengths=["Clean diff"],
        risks=["Race condition"],
    )


def make_result(*findings: ReviewFinding) -> ReviewResult:
    return ReviewResult.from_findings(
        pr_number=2,
        findings=list(findings),
        summary=make_summary(),
    )


# ---------------------------------------------------------------------------
# parse_changed_lines
# ---------------------------------------------------------------------------


class TestParseChangedLines:
    def test_empty_patch(self) -> None:
        assert parse_changed_lines("") == frozenset()

    def test_none_equivalent_empty_string(self) -> None:
        assert parse_changed_lines("") == frozenset()

    def test_added_lines_commentable(self) -> None:
        lines = parse_changed_lines(SAMPLE_PATCH)
        assert 11 in lines
        assert 12 in lines

    def test_context_lines_not_commentable(self) -> None:
        lines = parse_changed_lines(SAMPLE_PATCH)
        # Line 10 is the first context line in the hunk (new-file numbering).
        assert 10 not in lines

    def test_removed_lines_not_commentable(self) -> None:
        # Removed lines don't advance new-file counter.
        lines = parse_changed_lines(SAMPLE_PATCH)
        # There is no line at the removed-line position in the new file.
        assert lines == SAMPLE_COMMENTABLE

    def test_multiple_hunks(self) -> None:
        patch = (
            "@@ -1,3 +1,4 @@\n"
            " context\n"
            "+added1\n"
            " context2\n"
            "@@ -20,3 +21,4 @@\n"
            " ctx\n"
            "+added2\n"
            " ctx2\n"
        )
        lines = parse_changed_lines(patch)
        assert 2 in lines   # added1 is new-file line 2
        assert 22 in lines  # added2 is new-file line 22

    def test_no_added_lines(self) -> None:
        patch = "@@ -1,3 +1,3 @@\n context\n-removed\n context2\n"
        assert parse_changed_lines(patch) == frozenset()


# ---------------------------------------------------------------------------
# classify_findings
# ---------------------------------------------------------------------------


class TestClassifyFindings:
    def _index(self) -> dict[str, frozenset[int]]:
        return {"src/app.py": SAMPLE_COMMENTABLE}

    def test_valid_inline_finding(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 1
        assert inline[0].action == FindingAction.INLINE

    def test_invalid_file_downgraded_to_summary(self) -> None:
        f = make_finding(action="inline", file="nonexistent.py", line=11, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 0
        assert len(summary) == 1
        assert "not in PR diff" in summary[0].discard_reason

    def test_invalid_line_downgraded_to_summary(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=999, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 0
        assert len(summary) == 1
        assert "not a changed" in summary[0].discard_reason

    def test_unchanged_line_downgraded_to_summary(self) -> None:
        # Line 10 is a context line – NOT commentable.
        f = make_finding(action="inline", file="src/app.py", line=10, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(summary) == 1
        assert "not a changed" in summary[0].discard_reason

    def test_missing_line_downgraded_to_summary(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=None, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(summary) == 1
        assert "no line" in summary[0].discard_reason

    def test_missing_file_downgraded_to_summary(self) -> None:
        f = make_finding(action="inline", file=None, line=11, confidence=0.9)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(summary) == 1
        assert "no file" in summary[0].discard_reason

    def test_summary_finding_passes_through(self) -> None:
        f = make_finding(action="summary", file=None, line=None, confidence=0.6)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(summary) == 1
        assert summary[0].action == FindingAction.SUMMARY

    def test_low_confidence_discarded(self) -> None:
        f = make_finding(action="inline", confidence=0.4)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(discarded) == 1
        assert "threshold" in discarded[0].discard_reason

    def test_claude_discard_respected(self) -> None:
        # confidence ≥ 0.50 so the action-check rule fires, not the confidence rule.
        f = make_finding(action="discard", confidence=0.6)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(discarded) == 1
        assert "Claude action=discard" in discarded[0].discard_reason

    def test_inline_below_inline_threshold_downgraded_to_summary(self) -> None:
        # Confidence between 0.50 and 0.70 with action=inline → downgrade to summary.
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.6)
        result = make_result(f)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 0
        assert len(summary) == 1

    def test_one_bad_finding_does_not_invalidate_others(self) -> None:
        good = make_finding(finding_id="F001", action="inline", file="src/app.py",
                            line=11, confidence=0.9)
        bad = make_finding(finding_id="F002", action="inline", file="missing.py",
                           line=1, confidence=0.9)
        result = make_result(good, bad)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 1
        assert inline[0].finding.title == "Test finding"
        assert len(summary) == 1

    def test_multiple_inline_comments_all_classified(self) -> None:
        f1 = make_finding(finding_id="F001", action="inline", file="src/app.py",
                          line=11, confidence=0.9)
        f2 = make_finding(finding_id="F002", action="inline", file="src/app.py",
                          line=12, confidence=0.85)
        result = make_result(f1, f2)
        inline, summary, discarded = classify_findings(result, self._index())
        assert len(inline) == 2


# ---------------------------------------------------------------------------
# build_review_payload
# ---------------------------------------------------------------------------


class TestBuildReviewPayload:
    def _files(self) -> list[_FakeFile]:
        return [_FakeFile("src/app.py", SAMPLE_PATCH)]

    def test_payload_head_sha_preserved(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.9)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc123")
        assert payload.head_sha == "abc123"

    def test_inline_finding_produces_comment(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.9)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        assert len(payload.inline_comments) == 1
        comment = payload.inline_comments[0]
        assert comment["path"] == "src/app.py"
        assert comment["line"] == 11
        assert comment["side"] == "RIGHT"

    def test_multiple_inline_in_one_payload(self) -> None:
        f1 = make_finding(finding_id="F001", action="inline", file="src/app.py",
                          line=11, confidence=0.9)
        f2 = make_finding(finding_id="F002", action="inline", file="src/app.py",
                          line=12, confidence=0.85)
        result = make_result(f1, f2)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        assert len(payload.inline_comments) == 2

    def test_invalid_location_produces_no_comment(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=999, confidence=0.9)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        assert len(payload.inline_comments) == 0
        assert len(payload.summary) == 1

    def test_review_body_non_empty(self) -> None:
        f = make_finding(action="summary", file=None, line=None, confidence=0.6)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        assert len(payload.review_body) > 50

    def test_to_dict_json_compatible(self) -> None:
        import json
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.9)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        d = payload.to_dict()
        json.dumps(d)  # must not raise

    def test_to_dict_has_required_keys(self) -> None:
        f = make_finding(action="inline", file="src/app.py", line=11, confidence=0.9)
        result = make_result(f)
        payload = build_review_payload(result, self._files(), head_sha="abc")
        d = payload.to_dict()
        for key in ("pr_number", "head_sha", "inline_findings", "summary_findings",
                    "discarded_findings", "review_body", "inline_comments", "counts"):
            assert key in d, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# submit_pr_review MCP tool (mocked)
# ---------------------------------------------------------------------------

# We import the tool function directly for unit testing.
from server import submit_pr_review

OWNER = "testorg"
REPO = "testrepo"
PR_NUM = 2
PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{PR_NUM}"
BASE_URL = "https://api.github.com"

FAKE_PR = {
    "number": PR_NUM,
    "title": "Test PR",
    "body": "Description",
    "state": "open",
    "merged": False,
    "user": {"login": "author"},
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
    "merged_at": None,
    "commits": 1,
    "additions": 18,
    "deletions": 8,
    "changed_files": 1,
    "base": {"label": f"{OWNER}:main", "ref": "main", "sha": "base123"},
    "head": {"label": f"{OWNER}:feature", "ref": "feature", "sha": "head456"},
}

FAKE_FILES = [{
    "filename": "src/app.py",
    "status": "modified",
    "additions": 18,
    "deletions": 8,
    "changes": 26,
    "patch": SAMPLE_PATCH,
}]

FAKE_COMMITS = [{
    "sha": "abc111",
    "commit": {
        "message": "Add feature",
        "author": {"name": "Dev", "email": "dev@example.com", "date": "2024-01-01T00:00:00Z"},
    },
}]

FINDING_INLINE = ReviewFinding(
    finding_id="F001",
    category="correctness",
    severity="high",
    confidence=0.9,
    file="src/app.py",
    line=11,
    title="Off-by-one",
    description="Loop iterates too far.",
    recommendation="Use < instead of <=.",
    action="inline",
)

FINDING_SUMMARY = ReviewFinding(
    finding_id="F002",
    category="testing",
    severity="medium",
    confidence=0.6,
    file=None,
    line=None,
    title="Missing concurrency test",
    description="No test covers the concurrent path.",
    recommendation="Add a thread-safety test.",
    action="summary",
)

SUMMARY_OBJ = ReviewSummary(
    verdict="Request changes",
    overview="Two issues found.",
    strengths=[],
    risks=["Race condition"],
)

# Keep the dict form for the malformed-input tests below.
SUMMARY_DICT = {
    "verdict": "Request changes",
    "overview": "Two issues found.",
    "strengths": [],
    "risks": ["Race condition"],
}


def _mock_github_reads() -> None:
    """Register respx mocks for all read (GET) endpoints."""
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=FAKE_PR)
    )
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/files").mock(
        return_value=httpx.Response(200, json=FAKE_FILES)
    )
    respx.get(f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/commits").mock(
        return_value=httpx.Response(200, json=FAKE_COMMITS)
    )


class TestSubmitPrReviewTool:
    def test_dry_run_makes_no_post_request(self) -> None:
        """dry_run=True must issue zero POST/PUT/PATCH/DELETE requests."""
        with respx.mock:
            _mock_github_reads()
            # Intentionally do NOT register a POST mock.
            result = submit_pr_review(
                PR_URL,
                findings=[FINDING_INLINE],
                summary=SUMMARY_OBJ,
                dry_run=True,
            )
        assert result["dry_run"] is True
        assert result["submitted"] is False
        assert result["github_review_id"] is None

    def test_dry_run_current_head_sha_fetched(self) -> None:
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[FINDING_INLINE], summary=SUMMARY_OBJ, dry_run=True
            )
        # head_sha comes from the freshly fetched PR, not Claude's input.
        assert result["head_sha"] == "head456"

    def test_dry_run_valid_inline_classified_correctly(self) -> None:
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[FINDING_INLINE], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["inline"] == 1
        assert result["counts"]["summary"] == 0

    def test_dry_run_summary_finding_classified_correctly(self) -> None:
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[FINDING_SUMMARY], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["summary"] == 1
        assert result["counts"]["inline"] == 0

    def test_dry_run_invalid_file_downgraded_to_summary(self) -> None:
        bad_file = FINDING_INLINE.model_copy(update={"file": "nonexistent.py"})
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[bad_file], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["inline"] == 0
        assert result["counts"]["summary"] == 1

    def test_dry_run_invalid_line_downgraded_to_summary(self) -> None:
        bad_line = FINDING_INLINE.model_copy(update={"line": 9999})
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[bad_line], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["inline"] == 0
        assert result["counts"]["summary"] == 1

    def test_dry_run_low_confidence_discarded(self) -> None:
        low_conf = FINDING_INLINE.model_copy(update={"confidence": 0.3})
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[low_conf], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["discarded"] == 1
        assert result["counts"]["inline"] == 0

    def test_dry_run_multiple_inline_in_single_payload(self) -> None:
        f2 = FINDING_INLINE.model_copy(update={"finding_id": "F002", "line": 12})
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[FINDING_INLINE, f2], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["inline"] == 2
        assert len(result["inline_comments"]) == 2

    def test_dry_run_review_body_non_empty(self) -> None:
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[FINDING_INLINE], summary=SUMMARY_OBJ, dry_run=True
            )
        assert len(result["review_body"]) > 50

    def test_dry_run_malformed_finding_raises_value_error(self) -> None:
        """Invalid enum value in a finding dict is caught by Pydantic."""
        with pytest.raises((ValueError, Exception)):
            ReviewFinding.model_validate({"finding_id": "F001", "category": "not_valid"})

    def test_dry_run_malformed_summary_raises_value_error(self) -> None:
        """Missing required summary fields are caught by Pydantic."""
        with pytest.raises((ValueError, Exception)):
            ReviewSummary.model_validate({"wrong": "keys"})

    def test_dry_run_unchanged_line_downgraded_to_summary(self) -> None:
        # Line 10 is a context line (not +) in SAMPLE_PATCH – NOT commentable.
        unchanged = FINDING_INLINE.model_copy(update={"line": 10})
        with respx.mock:
            _mock_github_reads()
            result = submit_pr_review(
                PR_URL, findings=[unchanged], summary=SUMMARY_OBJ, dry_run=True
            )
        assert result["counts"]["inline"] == 0
        assert result["counts"]["summary"] == 1

    def test_real_submission_sends_one_post(self) -> None:
        """dry_run=False must issue exactly one POST to the reviews endpoint."""
        post_route = None
        with respx.mock:
            _mock_github_reads()
            post_route = respx.post(
                f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/reviews"
            ).mock(return_value=httpx.Response(200, json={"id": 999}))

            result = submit_pr_review(
                PR_URL, findings=[FINDING_INLINE], summary=SUMMARY_OBJ, dry_run=False
            )

        assert result["submitted"] is True
        assert result["github_review_id"] == 999
        assert post_route.called
        assert post_route.call_count == 1

    def test_github_submission_failure_raises_runtime_error(self) -> None:
        with respx.mock:
            _mock_github_reads()
            respx.post(
                f"{BASE_URL}/repos/{OWNER}/{REPO}/pulls/{PR_NUM}/reviews"
            ).mock(return_value=httpx.Response(422, json={"message": "Validation Failed"}))

            with pytest.raises(RuntimeError, match="422"):
                submit_pr_review(
                    PR_URL, findings=[FINDING_INLINE], summary=SUMMARY_OBJ, dry_run=False
                )


# ---------------------------------------------------------------------------
# Schema exposure tests
# ---------------------------------------------------------------------------


class TestSubmitPrReviewSchema:
    """Verify the MCP tool exposes a complete, unambiguous input schema to Claude."""

    def _get_schema(self) -> dict:
        from server import mcp
        tools = mcp._tool_manager.list_tools()
        for t in tools:
            if t.name == "submit_pr_review":
                return t.parameters
        raise AssertionError("submit_pr_review tool not found")

    def test_schema_has_defs_for_review_finding(self) -> None:
        schema = self._get_schema()
        assert "ReviewFinding" in schema.get("$defs", {})

    def test_schema_has_defs_for_review_summary(self) -> None:
        schema = self._get_schema()
        assert "ReviewSummary" in schema.get("$defs", {})

    def test_schema_has_defs_for_finding_category(self) -> None:
        schema = self._get_schema()
        assert "FindingCategory" in schema.get("$defs", {})

    def test_schema_has_defs_for_finding_severity(self) -> None:
        schema = self._get_schema()
        assert "FindingSeverity" in schema.get("$defs", {})

    def test_schema_has_defs_for_finding_action(self) -> None:
        schema = self._get_schema()
        assert "FindingAction" in schema.get("$defs", {})

    def test_category_enum_values_exposed(self) -> None:
        schema = self._get_schema()
        enum_vals = schema["$defs"]["FindingCategory"]["enum"]
        assert set(enum_vals) == {
            "correctness", "performance", "quality", "architecture",
            "security", "concurrency", "memory", "testing",
        }

    def test_severity_enum_values_exposed(self) -> None:
        schema = self._get_schema()
        enum_vals = schema["$defs"]["FindingSeverity"]["enum"]
        assert set(enum_vals) == {"critical", "high", "medium", "low"}

    def test_action_enum_values_exposed(self) -> None:
        schema = self._get_schema()
        enum_vals = schema["$defs"]["FindingAction"]["enum"]
        assert set(enum_vals) == {"inline", "summary", "discard"}

    def test_confidence_has_min_max_constraints(self) -> None:
        schema = self._get_schema()
        conf = schema["$defs"]["ReviewFinding"]["properties"]["confidence"]
        assert conf["minimum"] == 0.0
        assert conf["maximum"] == 1.0

    def test_finding_required_fields_listed(self) -> None:
        schema = self._get_schema()
        required = set(schema["$defs"]["ReviewFinding"]["required"])
        # Core required fields that Claude must always provide.
        assert {"category", "severity", "confidence", "title",
                "description", "recommendation", "action"} <= required

    def test_file_and_line_are_optional(self) -> None:
        schema = self._get_schema()
        required = set(schema["$defs"]["ReviewFinding"]["required"])
        assert "file" not in required
        assert "line" not in required

    def test_summary_required_fields_listed(self) -> None:
        schema = self._get_schema()
        required = set(schema["$defs"]["ReviewSummary"]["required"])
        assert {"verdict", "overview"} <= required

    def test_findings_param_references_review_finding(self) -> None:
        schema = self._get_schema()
        items = schema["properties"]["findings"]["items"]
        assert "$ref" in items
        assert "ReviewFinding" in items["$ref"]

    def test_summary_param_references_review_summary(self) -> None:
        schema = self._get_schema()
        summary_param = schema["properties"]["summary"]
        assert "$ref" in summary_param
        assert "ReviewSummary" in summary_param["$ref"]

    def test_dry_run_defaults_to_true(self) -> None:
        schema = self._get_schema()
        assert schema["properties"]["dry_run"]["default"] is True

    # --- Pydantic model validation (source of truth) ---

    def test_invalid_category_enum_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({
                "finding_id": "F001", "category": "not_a_category",
                "severity": "high", "confidence": 0.9,
                "title": "T", "description": "D", "recommendation": "R", "action": "inline",
            })

    def test_invalid_severity_enum_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({
                "finding_id": "F001", "category": "correctness",
                "severity": "extreme", "confidence": 0.9,
                "title": "T", "description": "D", "recommendation": "R", "action": "inline",
            })

    def test_invalid_action_enum_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({
                "finding_id": "F001", "category": "correctness",
                "severity": "high", "confidence": 0.9,
                "title": "T", "description": "D", "recommendation": "R", "action": "unknown",
            })

    def test_confidence_above_1_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({
                "finding_id": "F001", "category": "correctness",
                "severity": "high", "confidence": 1.5,
                "title": "T", "description": "D", "recommendation": "R", "action": "inline",
            })

    def test_confidence_below_0_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({
                "finding_id": "F001", "category": "correctness",
                "severity": "high", "confidence": -0.1,
                "title": "T", "description": "D", "recommendation": "R", "action": "inline",
            })

    def test_missing_required_finding_fields_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewFinding.model_validate({"finding_id": "F001"})

    def test_missing_required_summary_fields_rejected(self) -> None:
        with pytest.raises(Exception):
            ReviewSummary.model_validate({"strengths": ["nice"]})

    def test_valid_finding_accepted(self) -> None:
        f = ReviewFinding.model_validate({
            "finding_id": "F001",
            "category": "security",
            "severity": "critical",
            "confidence": 0.95,
            "file": "src/auth.py",
            "line": 42,
            "title": "SQL injection risk",
            "description": "User input concatenated directly into query.",
            "recommendation": "Use parameterised queries.",
            "action": "inline",
        })
        assert f.category.value == "security"
        assert f.severity.value == "critical"
        assert f.confidence == 0.95

    def test_valid_summary_accepted(self) -> None:
        s = ReviewSummary.model_validate({
            "verdict": "Request changes",
            "overview": "Two issues found.",
        })
        assert s.verdict == "Request changes"
        assert s.strengths == []
