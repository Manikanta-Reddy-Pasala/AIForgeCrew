"""Tests for the iterative reviewer<->doer handshake (gap A5).

Covers the pure helpers ``needs_revision`` / ``extract_fix_list`` and the
``review_rounds`` loop orchestrator. All LLM/gh side effects are injected
as fake callables so these tests stay hermetic.
"""
from __future__ import annotations

from aiforge_core.runtime.pr_reviewer import (
    extract_fix_list,
    needs_revision,
    review_rounds,
)


# --- needs_revision -------------------------------------------------------

def test_needs_revision_false_on_clean_review():
    review = {
        "verdict": "approve",
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    assert needs_revision(review) is False


def test_needs_revision_true_on_low_critical_axis():
    # correctness is a critical axis; 0/2 -> 0.0 normalized, below 0.7
    review = {
        "verdict": "request_changes",
        "axes": {"scope": 2, "correctness": 0, "security": 2,
                 "regression": 2, "style": 2},
    }
    assert needs_revision(review) is True


def test_needs_revision_false_when_only_noncritical_concern():
    # style is NOT critical; a concern there alone should not force revision
    review = {
        "verdict": "comment",
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 1},
    }
    assert needs_revision(review) is False


def test_needs_revision_true_on_low_overall_average():
    # all axes at concern (1/2 -> 0.5) drags the overall below threshold
    review = {
        "verdict": "comment",
        "axes": {"scope": 1, "correctness": 1, "security": 1,
                 "regression": 1, "style": 1},
    }
    assert needs_revision(review) is True


def test_needs_revision_respects_request_changes_verdict():
    # explicit request_changes should always need revision
    review = {
        "verdict": "request_changes",
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    assert needs_revision(review) is True


def test_needs_revision_empty_review_is_true():
    assert needs_revision({}) is True


def test_needs_revision_custom_threshold():
    review = {
        "axes": {"scope": 2, "correctness": 1, "security": 2,
                 "regression": 2, "style": 2},
    }
    # correctness 1/2 = 0.5; with a low min_score it passes
    assert needs_revision(review, min_score=0.4) is False
    assert needs_revision(review, min_score=0.6) is True


# --- extract_fix_list -----------------------------------------------------

def test_extract_fix_list_flags_low_axes():
    review = {
        "rationale": "needs work",
        "axes": {"scope": 2, "correctness": 0, "security": 1,
                 "regression": 2, "style": 2},
    }
    fixes = extract_fix_list(review)
    assert isinstance(fixes, list)
    joined = "\n".join(fixes).lower()
    assert "correctness" in joined
    assert "security" in joined
    # clean axes should not appear
    assert "regression" not in joined


def test_extract_fix_list_empty_when_all_clean():
    review = {
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    assert extract_fix_list(review) == []


def test_extract_fix_list_includes_axis_rationale():
    review = {
        "axes": {"correctness": 0},
        "correctness_rationale": "logic is inverted",
    }
    fixes = extract_fix_list(review)
    assert any("logic is inverted" in f for f in fixes)


# --- review_rounds --------------------------------------------------------

def test_review_rounds_approves_immediately_on_high_score():
    clean = {
        "verdict": "approve",
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    applied: list[list[str]] = []

    def run_review():
        return clean

    def apply_fixes(fix_list):
        applied.append(fix_list)

    result = review_rounds(run_review, apply_fixes)
    assert result["status"] == "approved"
    assert result["rounds"] == 1
    assert applied == []  # never needed to apply fixes


def test_review_rounds_iterates_then_stops_at_max_rounds():
    bad = {
        "verdict": "request_changes",
        "axes": {"scope": 0, "correctness": 0, "security": 0,
                 "regression": 0, "style": 0},
    }
    review_calls: list[int] = []
    applied: list[list[str]] = []

    def run_review():
        review_calls.append(1)
        return bad

    def apply_fixes(fix_list):
        applied.append(fix_list)

    result = review_rounds(run_review, apply_fixes, max_rounds=2)
    assert result["status"] == "max_rounds"
    assert result["rounds"] == 2
    assert len(review_calls) == 2
    # fixes applied after each failing round
    assert len(applied) == 2
    # the fix list handed to the doer is the extracted bullets
    assert all(isinstance(f, list) and f for f in applied)


def test_review_rounds_passes_fix_list_to_apply_fixes():
    bad = {
        "verdict": "request_changes",
        "axes": {"correctness": 0, "scope": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    captured: list[list[str]] = []

    def run_review():
        return bad

    def apply_fixes(fix_list):
        captured.append(fix_list)

    review_rounds(run_review, apply_fixes, max_rounds=1)
    assert captured, "apply_fixes was never called"
    joined = "\n".join(captured[0]).lower()
    assert "correctness" in joined


def test_review_rounds_recovers_after_fixes():
    bad = {
        "verdict": "request_changes",
        "axes": {"scope": 0, "correctness": 0, "security": 0,
                 "regression": 0, "style": 0},
    }
    good = {
        "verdict": "approve",
        "axes": {"scope": 2, "correctness": 2, "security": 2,
                 "regression": 2, "style": 2},
    }
    seq = [bad, good]

    def run_review():
        return seq.pop(0)

    def apply_fixes(fix_list):
        pass

    result = review_rounds(run_review, apply_fixes, max_rounds=3)
    assert result["status"] == "approved"
    assert result["rounds"] == 2
