"""Tests for Feedback verdict-event observability (ticket_events emit).

The Feedback agent's verdict + rationale must land in ``ticket_events``
with ``kind='verdict_attempt'``, ``agent_role='feedback'``, and a
``body`` of ``"<verdict>: <reason>"`` so operators can audit a
Doer-Feedback convergence path. These tests cover:

* :func:`_extract_reason` — pulls the rationale line out of every
  Feedback shape the parser tolerates (leading-token, legacy JSON
  string, legacy dict).
* :func:`_record_verdict_event` — calls :func:`tickets.store.add_event`
  with the canonical kind/role/body and survives DB hiccups
  (best-effort audit; the runner must still make forward progress).
"""
from __future__ import annotations

from unittest import mock

import pytest

from aiforge_core.runtime import adk_runner


# --------------------------------------------------------------------------- #
# _extract_reason — string + dict + JSON-string shapes                        #
# --------------------------------------------------------------------------- #

def test_extract_reason_leading_token_pass() -> None:
    state = {
        "feedback_verdict": (
            "pass\nDoer wrote LowStockSummaryService.java and "
            "mvn compile returned 0; meets acceptance."
        ),
    }
    out = adk_runner._extract_reason(state, "pass")
    assert out.startswith("Doer wrote LowStockSummaryService.java")
    assert "mvn compile returned 0" in out
    # newlines collapsed to single spaces for single-line audit row
    assert "\n" not in out


def test_extract_reason_leading_token_fail() -> None:
    state = {
        "feedback_verdict": (
            "fail\nCompile error in LowStockSummaryService.java:42 — "
            "missing import for ProductDao."
        ),
    }
    out = adk_runner._extract_reason(state, "fail")
    assert out.startswith("Compile error")
    assert "ProductDao" in out


def test_extract_reason_scope_violation_strips_token() -> None:
    """scope_violation contains 'fail' as substring; ensure the token
    is stripped from the head and the rationale starts with the path."""
    state = {
        "feedback_verdict": (
            "scope_violation\nDoer wrote /etc/passwd which is "
            "outside the workspace allowlist."
        ),
    }
    out = adk_runner._extract_reason(state, "scope_violation")
    assert out.startswith("Doer wrote /etc/passwd")
    assert "scope_violation" not in out.lower()


def test_extract_reason_legacy_dict_rationale() -> None:
    state = {
        "feedback_verdict": {
            "verdict": "pass",
            "rationale": "all green; mvn -q test exited 0",
        },
    }
    assert adk_runner._extract_reason(state, "pass") == \
        "all green; mvn -q test exited 0"


def test_extract_reason_legacy_dict_reason_alias() -> None:
    """Newer model output may use ``reason`` rather than ``rationale``."""
    state = {
        "feedback_verdict": {
            "verdict": "fail",
            "reason": "missing unit test for the negative path",
        },
    }
    assert adk_runner._extract_reason(state, "fail") == \
        "missing unit test for the negative path"


def test_extract_reason_legacy_json_string() -> None:
    state = {
        "feedback_verdict":
            '{"verdict": "fail", "rationale": "no test added"}',
    }
    assert adk_runner._extract_reason(state, "fail") == "no test added"


def test_extract_reason_no_rationale_uses_default() -> None:
    """A bare ``pass`` with no line 2 still emits a non-empty body so
    the audit row is greppable."""
    assert adk_runner._extract_reason({"feedback_verdict": "pass"}, "pass") == \
        adk_runner._REASON_DEFAULT_PASS
    assert adk_runner._extract_reason({"feedback_verdict": "fail"}, "fail") == \
        adk_runner._REASON_DEFAULT_FAIL


def test_extract_reason_truncates_at_300_chars() -> None:
    """Chatty model can't bloat ticket_events — cap at 300 chars
    with ellipsis so the audit row is bounded."""
    long_reason = "x" * 1000
    state = {"feedback_verdict": f"fail\n{long_reason}"}
    out = adk_runner._extract_reason(state, "fail")
    assert len(out) <= adk_runner._REASON_MAX_CHARS
    assert out.endswith("…")


def test_extract_reason_collapses_internal_whitespace() -> None:
    state = {
        "feedback_verdict":
            "fail\n  too\t\tmany\n   gaps   between   words  ",
    }
    out = adk_runner._extract_reason(state, "fail")
    assert out == "too many gaps between words"


def test_extract_reason_empty_state_uses_default() -> None:
    assert adk_runner._extract_reason({}, "pass") == \
        adk_runner._REASON_DEFAULT_PASS
    assert adk_runner._extract_reason({"feedback_verdict": None}, "fail") == \
        adk_runner._REASON_DEFAULT_FAIL


# --------------------------------------------------------------------------- #
# _record_verdict_event — calls tickets.store.add_event with canonical args   #
# --------------------------------------------------------------------------- #

def test_record_verdict_event_pass_emits_canonical_row() -> None:
    """Persists one row with kind=verdict_attempt, role=feedback, body
    formatted as ``<verdict>: <reason>``, and structured metadata so
    downstream queries can filter without parsing the body string."""
    with mock.patch.object(adk_runner.tickets_mod, "add_event") as mocked:
        adk_runner._record_verdict_event(
            42, "pass", "tests green; mvn -q test exited 0",
        )
    mocked.assert_called_once()
    args, kwargs = mocked.call_args
    # Positional signature:
    #   add_event(ticket_id, role, kind, body, metadata)
    assert args[0] == 42
    assert args[1] == "feedback"
    assert args[2] == "verdict_attempt"
    assert args[3] == "pass: tests green; mvn -q test exited 0"
    # metadata structured for SQL filters / dashboards
    metadata = args[4]
    assert metadata == {
        "verdict": "pass",
        "reason": "tests green; mvn -q test exited 0",
    }


def test_record_verdict_event_fail_emits_blocker_in_body() -> None:
    """Fail outcomes must surface the SINGLE blocker reason — operators
    grep ticket_events.body LIKE 'fail:%' to spot stuck loops."""
    with mock.patch.object(adk_runner.tickets_mod, "add_event") as mocked:
        adk_runner._record_verdict_event(
            99, "fail",
            "Compile error in LowStockSummaryService.java:42 — "
            "missing import for ProductDao",
        )
    args, _ = mocked.call_args
    assert args[2] == "verdict_attempt"
    assert args[3].startswith("fail: Compile error")
    assert args[1] == "feedback"


def test_record_verdict_event_scope_violation_emits_row() -> None:
    """scope_violation must also persist — operators need to see the
    off-allowlist path that triggered the cancel."""
    with mock.patch.object(adk_runner.tickets_mod, "add_event") as mocked:
        adk_runner._record_verdict_event(
            7, "scope_violation",
            "Doer wrote /etc/passwd outside workspace allowlist",
        )
    args, _ = mocked.call_args
    assert args[3] == (
        "scope_violation: Doer wrote /etc/passwd outside workspace allowlist"
    )


def test_record_verdict_event_swallows_db_errors() -> None:
    """Audit persistence is best-effort — a DB hiccup must NOT bubble
    out and break the runner's status-update path."""
    with mock.patch.object(
        adk_runner.tickets_mod, "add_event",
        side_effect=RuntimeError("postgres unreachable"),
    ):
        # Must not raise.
        adk_runner._record_verdict_event(1, "pass", "ok")


def test_record_verdict_event_uses_default_reason_when_empty() -> None:
    """Even with an empty reason string, a row is still persisted —
    a missing audit row is worse than a placeholder body."""
    with mock.patch.object(adk_runner.tickets_mod, "add_event") as mocked:
        adk_runner._record_verdict_event(5, "pass", "")
    mocked.assert_called_once()
    body = mocked.call_args.args[3]
    assert body.startswith("pass:")
