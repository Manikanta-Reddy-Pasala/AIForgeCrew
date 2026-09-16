"""A run that aborted was not judged, and the audit trail must say so.

On an abort the pipeline sets feedback_verdict="fail" so the ticket blocks —
correct — but the reason came out as "no rationale provided", and the row was
filed as the Feedback judge's verdict. Ticket #298 read "feedback: fail: no
rationale provided" while the model was unreachable and Feedback never ran.
"""
from __future__ import annotations

from aiforge_core.runtime.adk_runner import _verdict as v


def test_an_aborted_run_names_the_cause_not_a_missing_rationale():
    state = {"feedback_verdict": "fail", "_pipeline_abort": "RuntimeError",
             "_pipeline_abort_detail":
                 "LLM endpoint unreachable (192.168.70.185:8081): No route to host"}
    reason = v._extract_reason(state, "fail")
    assert reason != "no rationale provided"
    assert reason.startswith("not judged")
    assert "RuntimeError" in reason
    assert "No route to host" in reason


def test_a_deadline_abort_says_deadline():
    reason = v._extract_reason({"feedback_verdict": "fail",
                                "_pipeline_abort": "deadline"}, "fail")
    assert "wall-clock deadline" in reason


def test_a_long_cause_is_trimmed():
    state = {"feedback_verdict": "fail", "_pipeline_abort": "X",
             "_pipeline_abort_detail": "e" * 5000}
    assert len(v._extract_reason(state, "fail")) <= v._REASON_MAX_CHARS


def test_a_real_feedback_verdict_is_unchanged():
    reason = v._extract_reason({"feedback_verdict": "fail\nmvn test ran 0 tests"},
                               "fail")
    assert reason == "mvn test ran 0 tests"


def test_a_judge_that_gave_no_rationale_still_reads_as_before():
    assert v._extract_reason({"feedback_verdict": "fail"}, "fail") \
        == "no rationale provided"
