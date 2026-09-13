"""
tests/test_review_models.py
---------------------------
Tests for ReviewFinding, ReviewResult, and review/prompts.py.

No LLM calls, no network access.  Pure unit tests.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from review.models import (
    FindingAction,
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewSummary,
)
from review.prompts import (
    REVIEW_RUBRIC,
    RUBRIC_VERSION,
    get_review_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_finding(
    *,
    finding_id: str = "F001",
    category: str = "correctness",
    severity: str = "high",
    confidence: float = 0.85,
    file: str | None = "src/app.py",
    line: int | None = 42,
    title: str = "Off-by-one in loop bound",
    description: str = "The loop iterates one step too far.",
    recommendation: str = "Change `< len` to `<= len - 1`.",
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


def make_summary(
    *,
    verdict: str = "Request changes",
    overview: str = "One correctness issue found.",
    strengths: list[str] | None = None,
    risks: list[str] | None = None,
) -> ReviewSummary:
    return ReviewSummary(
        verdict=verdict,
        overview=overview,
        strengths=strengths or [],
        risks=risks or [],
    )


# ---------------------------------------------------------------------------
# Enum correctness
# ---------------------------------------------------------------------------


class TestEnums:
    def test_all_categories_present(self) -> None:
        expected = {
            "correctness", "performance", "quality", "architecture",
            "security", "concurrency", "memory", "testing",
        }
        actual = {c.value for c in FindingCategory}
        assert actual == expected

    def test_all_severities_present(self) -> None:
        expected = {"critical", "high", "medium", "low"}
        assert {s.value for s in FindingSeverity} == expected

    def test_all_actions_present(self) -> None:
        expected = {"inline", "summary", "discard"}
        assert {a.value for a in FindingAction} == expected

    def test_enum_values_are_lowercase_strings(self) -> None:
        for cat in FindingCategory:
            assert cat.value == cat.value.lower()
        for sev in FindingSeverity:
            assert sev.value == sev.value.lower()
        for act in FindingAction:
            assert act.value == act.value.lower()

    def test_enum_is_str_subclass(self) -> None:
        """Enums must be str subclasses so they serialise cleanly."""
        assert isinstance(FindingCategory.CORRECTNESS, str)
        assert isinstance(FindingSeverity.HIGH, str)
        assert isinstance(FindingAction.INLINE, str)


# ---------------------------------------------------------------------------
# ReviewFinding – valid construction
# ---------------------------------------------------------------------------


class TestReviewFindingValid:
    def test_basic_inline_finding(self) -> None:
        f = make_finding()
        assert f.finding_id == "F001"
        assert f.category == FindingCategory.CORRECTNESS
        assert f.severity == FindingSeverity.HIGH
        assert f.confidence == 0.85
        assert f.file == "src/app.py"
        assert f.line == 42
        assert f.action == FindingAction.INLINE

    def test_summary_finding_no_location(self) -> None:
        f = make_finding(action="summary", file=None, line=None)
        assert f.action == FindingAction.SUMMARY
        assert f.file is None
        assert f.line is None

    def test_discard_finding(self) -> None:
        f = make_finding(action="discard", confidence=0.3)
        assert f.action == FindingAction.DISCARD

    def test_confidence_boundary_zero(self) -> None:
        f = make_finding(confidence=0.0)
        assert f.confidence == 0.0

    def test_confidence_boundary_one(self) -> None:
        f = make_finding(confidence=1.0)
        assert f.confidence == 1.0

    def test_finding_id_three_digits(self) -> None:
        f = make_finding(finding_id="F042")
        assert f.finding_id == "F042"

    def test_finding_id_many_digits(self) -> None:
        f = make_finding(finding_id="F1000")
        assert f.finding_id == "F1000"

    def test_all_categories_accepted(self) -> None:
        for cat in FindingCategory:
            f = make_finding(category=cat.value)
            assert f.category == cat

    def test_all_severities_accepted(self) -> None:
        for sev in FindingSeverity:
            f = make_finding(severity=sev.value)
            assert f.severity == sev

    def test_all_actions_accepted(self) -> None:
        for act in FindingAction:
            f = make_finding(action=act.value, file=None, line=None)
            assert f.action == act

    def test_to_dict_excludes_none(self) -> None:
        f = make_finding(file=None, line=None, action="summary")
        d = f.to_dict()
        assert "file" not in d
        assert "line" not in d

    def test_to_dict_includes_all_required_keys(self) -> None:
        f = make_finding()
        d = f.to_dict()
        for key in ("finding_id", "category", "severity", "confidence",
                    "title", "description", "recommendation", "action"):
            assert key in d, f"Missing key: {key}"

    def test_json_serialisable(self) -> None:
        f = make_finding()
        serialised = json.dumps(f.to_dict())
        assert serialised


# ---------------------------------------------------------------------------
# ReviewFinding – invalid inputs
# ---------------------------------------------------------------------------


class TestReviewFindingInvalid:
    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            make_finding(category="not_a_category")
        assert "category" in str(exc_info.value).lower() or "not_a_category" in str(exc_info.value)

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            make_finding(severity="extreme")
        assert "severity" in str(exc_info.value).lower() or "extreme" in str(exc_info.value)

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            make_finding(action="post_comment")
        assert "action" in str(exc_info.value).lower() or "post_comment" in str(exc_info.value)

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_finding(confidence=1.01)

    def test_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_finding(confidence=-0.01)

    def test_confidence_exactly_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_finding(confidence=-1.0)

    def test_invalid_finding_id_format_rejected(self) -> None:
        """finding_id must match F<digits>."""
        for bad_id in ("001", "finding-1", "f001", "F", "F00A", ""):
            with pytest.raises(ValidationError, match="finding_id"):
                make_finding(finding_id=bad_id)

    def test_zero_line_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_finding(line=0)

    def test_negative_line_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_finding(line=-5)

    def test_missing_required_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewFinding(finding_id="F001")  # many required fields missing


# ---------------------------------------------------------------------------
# ReviewSummary
# ---------------------------------------------------------------------------


class TestReviewSummary:
    def test_basic_summary(self) -> None:
        s = make_summary()
        assert s.verdict == "Request changes"
        assert s.overview

    def test_strengths_and_risks_optional(self) -> None:
        s = ReviewSummary(verdict="LGTM", overview="Looks good.")
        assert s.strengths == []
        assert s.risks == []

    def test_strengths_and_risks_populated(self) -> None:
        s = make_summary(
            strengths=["Good tests", "Clean diff"],
            risks=["Race condition in payment handler"],
        )
        assert len(s.strengths) == 2
        assert len(s.risks) == 1


# ---------------------------------------------------------------------------
# ReviewResult – from_findings factory
# ---------------------------------------------------------------------------


class TestReviewResultFromFindings:
    def _findings(self) -> list[ReviewFinding]:
        return [
            make_finding(finding_id="F999", action="inline"),   # ID will be overwritten
            make_finding(finding_id="F999", action="summary", file=None, line=None),
            make_finding(finding_id="F999", action="discard", confidence=0.3),
        ]

    def test_ids_assigned_sequentially(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=1,
            findings=self._findings(),
            summary=make_summary(),
        )
        ids = [f.finding_id for f in result.findings]
        assert ids == ["F001", "F002", "F003"]

    def test_ids_padded_to_three_digits(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=1,
            findings=self._findings(),
            summary=make_summary(),
        )
        assert result.findings[0].finding_id == "F001"

    def test_counts_correct(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=1,
            findings=self._findings(),
            summary=make_summary(),
        )
        assert result.total_findings == 3
        assert result.inline_count == 1
        assert result.summary_count == 1
        assert result.discard_count == 1

    def test_pr_number_preserved(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=42,
            findings=[],
            summary=make_summary(),
        )
        assert result.pr_number == 42

    def test_empty_findings(self) -> None:
        result = ReviewResult.from_findings(
            pr_number=1,
            findings=[],
            summary=make_summary(),
        )
        assert result.findings == []
        assert result.total_findings == 0

    def test_caller_objects_not_mutated(self) -> None:
        """from_findings must not mutate the caller's finding objects."""
        originals = self._findings()
        original_ids = [f.finding_id for f in originals]
        ReviewResult.from_findings(pr_number=1, findings=originals, summary=make_summary())
        # Originals must still have their old placeholder IDs.
        assert [f.finding_id for f in originals] == original_ids

    def test_large_review_ids_above_f009(self) -> None:
        """IDs beyond F009 must still be padded correctly: F010, not F10."""
        many = [make_finding(finding_id="F001", action="summary", file=None, line=None)
                for _ in range(12)]
        result = ReviewResult.from_findings(pr_number=1, findings=many, summary=make_summary())
        assert result.findings[9].finding_id == "F010"
        assert result.findings[11].finding_id == "F012"


# ---------------------------------------------------------------------------
# ReviewResult – filtering helpers
# ---------------------------------------------------------------------------


class TestReviewResultHelpers:
    def _result(self) -> ReviewResult:
        findings = [
            make_finding(finding_id="F001", action="inline"),
            make_finding(finding_id="F002", action="summary", file=None, line=None),
            make_finding(finding_id="F003", action="discard", confidence=0.2),
            make_finding(finding_id="F004", action="inline"),
        ]
        return ReviewResult.from_findings(pr_number=5, findings=findings, summary=make_summary())

    def test_active_findings_excludes_discarded(self) -> None:
        result = self._result()
        active = result.active_findings()
        assert len(active) == 3
        assert all(f.action != FindingAction.DISCARD for f in active)

    def test_inline_findings_only_inline(self) -> None:
        result = self._result()
        inline = result.inline_findings()
        assert len(inline) == 2
        assert all(f.action == FindingAction.INLINE for f in inline)

    def test_summary_findings_only_summary(self) -> None:
        result = self._result()
        summ = result.summary_findings()
        assert len(summ) == 1
        assert summ[0].action == FindingAction.SUMMARY


# ---------------------------------------------------------------------------
# ReviewResult – serialisation
# ---------------------------------------------------------------------------


class TestReviewResultSerialisation:
    def _result(self) -> ReviewResult:
        return ReviewResult.from_findings(
            pr_number=7,
            findings=[make_finding(finding_id="F001", action="inline")],
            summary=make_summary(
                verdict="Request changes",
                overview="A correctness issue was found.",
                strengths=["Good test coverage"],
                risks=["Off-by-one error"],
            ),
        )

    def test_to_dict_json_serialisable(self) -> None:
        d = self._result().to_dict()
        serialised = json.dumps(d)
        assert serialised

    def test_to_dict_has_required_keys(self) -> None:
        d = self._result().to_dict()
        for key in ("pr_number", "findings", "summary", "total_findings"):
            assert key in d, f"Missing key: {key}"

    def test_findings_in_dict_are_dicts(self) -> None:
        d = self._result().to_dict()
        assert all(isinstance(f, dict) for f in d["findings"])

    def test_roundtrip_via_json(self) -> None:
        """Serialise to JSON and re-parse back to ReviewResult."""
        original = self._result()
        json_str = json.dumps(original.to_dict())
        parsed = ReviewResult.model_validate(json.loads(json_str))
        assert parsed.pr_number == original.pr_number
        assert len(parsed.findings) == len(original.findings)
        assert parsed.findings[0].finding_id == "F001"
        assert parsed.summary.verdict == original.summary.verdict

    def test_category_enum_serialises_as_string(self) -> None:
        d = self._result().to_dict()
        finding_dict = d["findings"][0]
        assert isinstance(finding_dict["category"], str)
        assert finding_dict["category"] == "correctness"

    def test_severity_enum_serialises_as_string(self) -> None:
        d = self._result().to_dict()
        assert isinstance(d["findings"][0]["severity"], str)

    def test_action_enum_serialises_as_string(self) -> None:
        d = self._result().to_dict()
        assert isinstance(d["findings"][0]["action"], str)


# ---------------------------------------------------------------------------
# review/prompts.py
# ---------------------------------------------------------------------------


class TestReviewPrompts:
    def test_rubric_is_non_empty_string(self) -> None:
        assert isinstance(REVIEW_RUBRIC, str)
        assert len(REVIEW_RUBRIC) > 500

    def test_rubric_version_format(self) -> None:
        parts = RUBRIC_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_rubric_contains_all_categories(self) -> None:
        rubric_upper = REVIEW_RUBRIC.upper()
        for category in ("CORRECTNESS", "PERFORMANCE", "QUALITY", "ARCHITECTURE",
                         "SECURITY", "CONCURRENCY", "MEMORY", "TESTING"):
            assert category in rubric_upper, f"Category {category} missing from rubric"

    def test_rubric_contains_severity_levels(self) -> None:
        rubric_upper = REVIEW_RUBRIC.upper()
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            assert sev in rubric_upper, f"Severity {sev} missing from rubric"

    def test_rubric_contains_action_keywords(self) -> None:
        assert "INLINE" in REVIEW_RUBRIC.upper()
        assert "SUMMARY" in REVIEW_RUBRIC.upper()
        assert "DISCARD" in REVIEW_RUBRIC.upper()

    def test_rubric_contains_exclusion_guidance(self) -> None:
        rubric_lower = REVIEW_RUBRIC.lower()
        assert "do not report" in rubric_lower or "not to report" in rubric_lower

    def test_rubric_references_line_number_integrity(self) -> None:
        """Rubric must warn against inventing line numbers."""
        assert "invent" in REVIEW_RUBRIC.lower() or "do not" in REVIEW_RUBRIC.lower()

    def test_rubric_references_confidence(self) -> None:
        assert "confidence" in REVIEW_RUBRIC.lower()

    def test_rubric_references_race_condition(self) -> None:
        assert "race condition" in REVIEW_RUBRIC.lower()

    def test_rubric_references_deadlock(self) -> None:
        assert "deadlock" in REVIEW_RUBRIC.lower()

    def test_rubric_references_resource_leak(self) -> None:
        assert "resource leak" in REVIEW_RUBRIC.lower() or "resource leaks" in REVIEW_RUBRIC.lower()

    def test_get_review_prompt_returns_rubric(self) -> None:
        prompt = get_review_prompt()
        assert prompt == REVIEW_RUBRIC

    def test_get_review_prompt_is_string(self) -> None:
        assert isinstance(get_review_prompt(), str)
        assert len(get_review_prompt()) > 500
