"""
tests/test_prompt.py
--------------------
Focused tests that verify:
  1. The pr_review_rubric prompt is registered with the FastMCP server.
  2. prompts/list would expose it (PromptManager.list_prompts).
  3. Calling the prompt returns the full rubric text.
"""

from __future__ import annotations

import pytest

import server
from review.prompts import REVIEW_RUBRIC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_registered_prompt(name: str):
    """Return the registered Prompt object from the FastMCP server, or None."""
    pm = server.mcp._prompt_manager
    for p in pm.list_prompts():
        if p.name == name:
            return p
    return None


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestPromptRegistration:

    def test_prompt_is_registered(self):
        """pr_review_rubric must appear in prompts/list."""
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None, (
            "pr_review_rubric is not registered. "
            "Check that @mcp.prompt() decoration is applied in server.py."
        )

    def test_prompt_has_description(self):
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None
        assert p.description.strip() != "", "Prompt description must not be empty."

    def test_prompt_has_no_required_arguments(self):
        """Simplified prompt takes no arguments."""
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None
        required_args = [a for a in p.arguments if a.required]
        assert required_args == [], f"Unexpected required arguments: {required_args}"


# ---------------------------------------------------------------------------
# Callable / return-value tests
# ---------------------------------------------------------------------------

class TestPromptCallable:

    def test_call_returns_rubric_string(self):
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None
        result = p.fn()
        assert isinstance(result, str)
        assert len(result) > 500
        assert "ReviewResult" in result
        assert "ReviewFinding" in result

    def test_call_contains_all_categories(self):
        """The rubric must mention all review categories."""
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None
        result = p.fn()
        for category in ["CORRECTNESS", "PERFORMANCE", "SECURITY", "CONCURRENCY", "TESTING"]:
            assert category in result, f"Rubric missing category: {category}"

    def test_call_matches_review_rubric_constant(self):
        p = _get_registered_prompt("pr_review_rubric")
        assert p is not None
        assert p.fn() == REVIEW_RUBRIC


# ---------------------------------------------------------------------------
# Protocol-level: prompts/list shape test
# ---------------------------------------------------------------------------

class TestPromptsListShape:
    """
    Verifies what the MCP client would receive for the prompts/list response.
    """

    def test_list_prompts_contains_pr_review_rubric(self):
        pm = server.mcp._prompt_manager
        names = [p.name for p in pm.list_prompts()]
        assert "pr_review_rubric" in names

    def test_list_prompts_has_exactly_one_prompt(self):
        pm = server.mcp._prompt_manager
        names = [p.name for p in pm.list_prompts()]
        assert names.count("pr_review_rubric") == 1
