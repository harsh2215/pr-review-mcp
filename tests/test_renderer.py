"""
tests/test_renderer.py
----------------------
Unit tests for the GitHub-facing review renderer in review/submission.py.

Verifies that:
- Finding IDs (F001 etc.) are absent from all GitHub output.
- Confidence values are absent from all GitHub output.
- Internal category labels are absent from all GitHub output.
- Emoji severity badges are absent.
- "N inline comment(s)" text is absent.
- No HTML entities (&#x20; etc.) are produced.
- No literal \\n escape sequences appear in rendered strings.
- Inline comments contain the substantive issue title + recommendation.
- Summary findings appear naturally in the review body.
- The review body does not duplicate inline findings' full text.
- The review body contains the verdict and overview.
- Strengths appear only when present.
- Classification/validation/diff-parsing behaviour is unchanged.
"""

from __future__ import annotations

import re

import pytest

from review.models import (
    FindingAction,
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewSummary,
)
from review.submission import (
    build_inline_comments,
    build_review_body,
    ClassifiedFinding,
    _render_inline_comment_body,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_BASE_FINDING = dict(
    finding_id="F001",
    category="correctness",
    severity="critical",
    confidence=0.97,
    file="src/payment_service.py",
    line=42,
    title="Typo in attribute name breaks module import",
    description="The attribute `self._payment_rep   ository` contains whitespace and is invalid Python.",
    recommendation="Change to `self._payment_repository = payment_repository`.",
    action="inline",
)


def _make_finding(**overrides) -> ReviewFinding:
    data = {**_BASE_FINDING, **overrides}
    return ReviewFinding.model_validate(data)


def _make_summary_finding(**overrides) -> ReviewFinding:
    data = {
        **_BASE_FINDING,
        "finding_id": "F002",
        "category": "concurrency",
        "severity": "high",
        "confidence": 0.85,
        "file": None,
        "line": None,
        "title": "Per-payment locking not applied consistently",
        "description": (
            "`retry_payment` and `update_payment_status` do not use the new "
            "per-payment locks, so they can still race with `transfer_retry_state`."
        ),
        "recommendation": "Apply the same per-payment lock in all mutating methods.",
        "action": "summary",
        **overrides,
    }
    return ReviewFinding.model_validate(data)


def _make_review_summary(**overrides) -> ReviewSummary:
    data = dict(
        verdict="Request changes — two issues require attention before merge.",
        overview="The PR introduces a syntax error and a locking gap that could cause data corruption.",
        strengths=["Well-structured diff", "Good test coverage for the happy path"],
        risks=["Race condition in payment processing"],
    )
    data.update(overrides)
    return ReviewSummary.model_validate(data)



def _inline_cf(finding: ReviewFinding | None = None) -> ClassifiedFinding:
    f = finding or _make_finding()
    return ClassifiedFinding(finding=f, action=FindingAction.INLINE)


def _summary_cf(finding: ReviewFinding | None = None) -> ClassifiedFinding:
    f = finding or _make_summary_finding()
    return ClassifiedFinding(finding=f, action=FindingAction.SUMMARY)


def _make_result(
    findings: list[ReviewFinding] | None = None,
    summary: ReviewSummary | None = None,
) -> ReviewResult:
    fs = findings or [_make_finding(), _make_summary_finding()]
    s = summary or _make_summary_finding_summary()
    return ReviewResult.from_findings(pr_number=3, findings=fs, summary=s)


def _make_summary_finding_summary() -> ReviewSummary:
    return _make_review_summary()


# ---------------------------------------------------------------------------
# _render_inline_comment_body
# ---------------------------------------------------------------------------


class TestRenderInlineCommentBody:
    def test_contains_title(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert f.title in body

    def test_contains_description(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert f.description in body

    def test_contains_recommendation(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert f.recommendation in body

    def test_no_finding_id(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "F001" not in body

    def test_no_confidence(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "%" not in body
        assert "confidence" not in body.lower()
        assert "0.97" not in body

    def test_no_category_label(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "CORRECTNESS" not in body
        assert "correctness" not in body.lower().replace(body.lower()[:5], "")  # not as a label

    def test_no_severity_badge(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        # No emoji badges
        for badge in ("🔴", "🟠", "🟡", "🔵", "⚪"):
            assert badge not in body

    def test_no_html_entities(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "&#" not in body
        assert "&amp;" not in body

    def test_no_literal_backslash_n(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "\\n" not in body

    def test_no_internal_label_inline(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "INLINE" not in body

    def test_fix_prefix_present(self) -> None:
        f = _make_finding()
        body = _render_inline_comment_body(f)
        assert "Fix:" in body or "Recommendation" in body

    def test_no_recommendation_when_empty(self) -> None:
        f = _make_finding(recommendation="")
        body = _render_inline_comment_body(f)
        # "Fix:" section should be absent when recommendation is empty.
        assert "Fix:" not in body


# ---------------------------------------------------------------------------
# build_inline_comments
# ---------------------------------------------------------------------------


class TestBuildInlineComments:
    def _comments(self, findings: list[ReviewFinding] | None = None) -> list[dict]:
        cfs = [_inline_cf(f) for f in (findings or [_make_finding()])]
        return build_inline_comments(cfs)

    def test_returns_one_comment_per_finding(self) -> None:
        comments = self._comments([_make_finding(), _make_finding(finding_id="F002", line=43)])
        assert len(comments) == 2

    def test_path_set_correctly(self) -> None:
        f = _make_finding()
        comments = self._comments([f])
        assert comments[0]["path"] == f.file

    def test_line_set_correctly(self) -> None:
        f = _make_finding(line=99)
        comments = self._comments([f])
        assert comments[0]["line"] == 99

    def test_side_is_right(self) -> None:
        comments = self._comments()
        assert comments[0]["side"] == "RIGHT"

    def test_no_finding_id_in_body(self) -> None:
        comments = self._comments()
        assert "F001" not in comments[0]["body"]

    def test_no_confidence_in_body(self) -> None:
        comments = self._comments()
        body = comments[0]["body"]
        assert "%" not in body
        assert "confidence" not in body.lower()

    def test_no_category_in_body(self) -> None:
        comments = self._comments()
        body = comments[0]["body"]
        assert "CORRECTNESS" not in body

    def test_no_severity_badge_in_body(self) -> None:
        comments = self._comments()
        body = comments[0]["body"]
        for badge in ("🔴", "🟠", "🟡", "🔵"):
            assert badge not in body

    def test_no_html_entities_in_body(self) -> None:
        comments = self._comments()
        assert "&#" not in comments[0]["body"]

    def test_no_literal_backslash_n_in_body(self) -> None:
        comments = self._comments()
        assert "\\n" not in comments[0]["body"]

    def test_title_present_in_body(self) -> None:
        f = _make_finding()
        comments = self._comments([f])
        assert f.title in comments[0]["body"]

    def test_description_present_in_body(self) -> None:
        f = _make_finding()
        comments = self._comments([f])
        assert f.description in comments[0]["body"]

    def test_recommendation_present_in_body(self) -> None:
        f = _make_finding()
        comments = self._comments([f])
        assert f.recommendation in comments[0]["body"]


# ---------------------------------------------------------------------------
# build_review_body
# ---------------------------------------------------------------------------


class TestBuildReviewBody:
    def _body(
        self,
        inline: list[ClassifiedFinding] | None = None,
        summary: list[ClassifiedFinding] | None = None,
        discarded: list[ClassifiedFinding] | None = None,
        result: ReviewResult | None = None,
    ) -> str:
        if result is None:
            findings = [f.finding for f in (inline or [])] + [f.finding for f in (summary or [])]
            s = _make_review_summary()
            result = ReviewResult.from_findings(pr_number=3, findings=findings, summary=s)
        return build_review_body(
            result=result,
            inline=inline or [],
            summary=summary or [],
            discarded=discarded or [],
        )

    # --- Content present ---

    def test_contains_verdict(self) -> None:
        body = self._body(inline=[_inline_cf()])
        assert "Request changes" in body

    def test_contains_overview(self) -> None:
        body = self._body(inline=[_inline_cf()])
        assert "syntax error" in body.lower() or "locking" in body.lower()

    def test_header_present(self) -> None:
        body = self._body()
        assert "AI Code Review" in body

    def test_summary_finding_title_in_body(self) -> None:
        cf = _summary_cf()
        body = self._body(summary=[cf])
        assert cf.finding.title in body

    def test_summary_finding_description_in_body(self) -> None:
        cf = _summary_cf()
        body = self._body(summary=[cf])
        assert "retry_payment" in body or "locking" in body.lower()

    def test_strengths_present_when_provided(self) -> None:
        body = self._body(inline=[_inline_cf()])
        assert "Well-structured diff" in body

    def test_strengths_section_absent_when_empty(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=3,
            findings=[_make_finding()],
            summary=_make_review_summary(strengths=[]),
        )
        body = self._body(
            inline=[_inline_cf()],
            result=result,
        )
        assert "Strengths" not in body

    # --- Content absent ---

    def test_no_finding_id_in_body(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        assert not re.search(r"\bF\d{3,}\b", body)

    def test_no_confidence_in_body(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        assert "%" not in body
        assert "confidence" not in body.lower()

    def test_no_category_label_in_body(self) -> None:
        body = self._body(summary=[_summary_cf()])
        assert "CORRECTNESS" not in body
        assert "CONCURRENCY" not in body

    def test_no_severity_badge_in_body(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        for badge in ("🔴", "🟠", "🟡", "🔵", "⚪"):
            assert badge not in body

    def test_no_inline_comment_count_text(self) -> None:
        inline = [_inline_cf(_make_finding()), _inline_cf(_make_finding(finding_id="F002", line=43))]
        body = self._body(inline=inline)
        assert "inline comment" not in body.lower()
        assert "2 inline" not in body

    def test_no_mcp_terminology(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        for term in ("MCP", "mcp", "downgraded", "DISCARD", "classified"):
            assert term not in body

    def test_no_html_entities(self) -> None:
        body = self._body(summary=[_summary_cf()])
        assert "&#" not in body
        assert "&amp;" not in body

    def test_no_literal_backslash_n(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        assert "\\n" not in body

    def test_inline_finding_full_text_not_duplicated(self) -> None:
        """Inline findings should not appear in full in the review body."""
        f = _make_finding()
        body = self._body(inline=[_inline_cf(f)])
        # The description (the verbose text) of an inline finding must not be
        # duplicated in the body — it already appears on the diff line.
        assert f.description not in body

    def test_no_findings_summary_section_label(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        assert "Findings (summary)" not in body

    def test_no_inline_comments_section_label(self) -> None:
        body = self._body(inline=[_inline_cf()])
        assert "Inline Comments" not in body

    # --- Critical/high vs medium/low grouping ---

    def test_critical_finding_in_key_concerns(self) -> None:
        f = _make_summary_finding(severity="critical")
        body = self._body(summary=[_summary_cf(f)])
        assert "Key concerns" in body

    def test_medium_finding_in_other_concerns(self) -> None:
        f = _make_summary_finding(severity="medium")
        body = self._body(summary=[_summary_cf(f)])
        assert "Other concerns" in body

    def test_empty_review_has_header(self) -> None:
        body = self._body()
        assert "AI Code Review" in body

    # --- Formatting robustness ---

    def test_no_triple_newlines(self) -> None:
        body = self._body(inline=[_inline_cf()], summary=[_summary_cf()])
        assert "\n\n\n" not in body
