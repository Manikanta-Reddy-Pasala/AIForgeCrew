"""Unit tests for aiforge_core.legacy.graph.edges — Phase 3.

All tests are pure: AgentState dict in, node-name string out.
No I/O, no Postgres, no LLM.
"""
from __future__ import annotations

import pytest

from aiforge_core.legacy.graph.state import AgentState


def _base_state(**overrides) -> AgentState:
    base: AgentState = {
        "ticket_id": 1,
        "ticket": {"assignee_role": "planner", "metadata": {}},
        "role": "supervisor",
        "messages": [],
        "tool_results": [],
        "worktree_path": None,
        "stop_reason": None,
        "compile_fail_count": 0,
        "verdict": None,
        "feedback_fixlist": None,
        "learner_digest": None,
        "flags": {},
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# ─────────────────────────── route_from_supervisor ──────────────────────

class TestRouteFromSupervisor:
    def test_routes_to_planner_when_assignee_planner(self) -> None:
        from aiforge_core.legacy.graph.edges import route_from_supervisor
        state = _base_state(ticket={"assignee_role": "planner", "metadata": {}})
        assert route_from_supervisor(state) == "planner_node"

    def test_routes_to_doer_when_assignee_doer(self) -> None:
        from aiforge_core.legacy.graph.edges import route_from_supervisor
        state = _base_state(ticket={"assignee_role": "doer", "metadata": {}})
        assert route_from_supervisor(state) == "doer_node"

    def test_routes_to_learner_when_assignee_learner(self) -> None:
        from aiforge_core.legacy.graph.edges import route_from_supervisor
        state = _base_state(ticket={"assignee_role": "learner", "metadata": {}})
        assert route_from_supervisor(state) == "learner_node"

    def test_routes_to_end_when_stop_reason_done(self) -> None:
        from langgraph.graph import END
        from aiforge_core.legacy.graph.edges import route_from_supervisor
        state = _base_state(stop_reason="done")
        assert route_from_supervisor(state) == END

    def test_routes_legacy_alias_sr_developer_to_planner(self) -> None:
        from aiforge_core.legacy.graph.edges import route_from_supervisor
        state = _base_state(ticket={"assignee_role": "sr_developer", "metadata": {}})
        assert route_from_supervisor(state) == "planner_node"


# ─────────────────────────── after_doer ─────────────────────────────────

class TestAfterDoer:
    def test_routes_to_feedback_normally(self) -> None:
        from aiforge_core.legacy.graph.edges import after_doer
        state = _base_state(compile_fail_count=0, stop_reason=None)
        assert after_doer(state) == "feedback_node"

    def test_escalates_to_planner_on_two_compile_fails(self) -> None:
        from aiforge_core.legacy.graph.edges import after_doer
        state = _base_state(compile_fail_count=2, stop_reason=None)
        assert after_doer(state) == "planner_node"

    def test_escalates_to_planner_on_more_than_two_fails(self) -> None:
        from aiforge_core.legacy.graph.edges import after_doer
        state = _base_state(compile_fail_count=5, stop_reason=None)
        assert after_doer(state) == "planner_node"

    def test_routes_to_end_on_blocked(self) -> None:
        from langgraph.graph import END
        from aiforge_core.legacy.graph.edges import after_doer
        state = _base_state(compile_fail_count=0, stop_reason="blocked")
        assert after_doer(state) == END

    def test_one_compile_fail_still_goes_to_feedback(self) -> None:
        from aiforge_core.legacy.graph.edges import after_doer
        state = _base_state(compile_fail_count=1, stop_reason=None)
        assert after_doer(state) == "feedback_node"


# ─────────────────────────── after_feedback ─────────────────────────────

class TestAfterFeedback:
    def test_pass_routes_to_learner(self) -> None:
        from aiforge_core.legacy.graph.edges import after_feedback
        state = _base_state(verdict="pass", stop_reason=None)
        assert after_feedback(state) == "learner_node"

    def test_fail_routes_to_doer(self) -> None:
        from aiforge_core.legacy.graph.edges import after_feedback
        state = _base_state(verdict="fail", stop_reason=None)
        assert after_feedback(state) == "doer_node"

    def test_scope_violation_routes_to_end(self) -> None:
        from langgraph.graph import END
        from aiforge_core.legacy.graph.edges import after_feedback
        state = _base_state(verdict="scope_violation", stop_reason=None)
        assert after_feedback(state) == END

    def test_none_verdict_routes_to_doer(self) -> None:
        from aiforge_core.legacy.graph.edges import after_feedback
        state = _base_state(verdict=None, stop_reason=None)
        assert after_feedback(state) == "doer_node"

    def test_stop_reason_done_routes_to_end(self) -> None:
        from langgraph.graph import END
        from aiforge_core.legacy.graph.edges import after_feedback
        state = _base_state(verdict="pass", stop_reason="done")
        assert after_feedback(state) == END
