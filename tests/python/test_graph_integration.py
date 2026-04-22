"""Integration test: full Supervisor→Planner→Doer→Feedback→Learner pipeline.

Uses mock LLM, mock Postgres (no real DB), and an in-memory graph compile.
All I/O is stubbed — fully offline.
"""
from __future__ import annotations

import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.graph.state import AgentState


# ─────────────────────────── Ticket stub ────────────────────────────────

@dataclass
class _FakeTicket:
    id: int = 1
    identifier: str = "ONE-99"
    title: str = "Test ticket"
    body: str = "## Files\n- src/Foo.java\n"
    status: str = "in_progress"
    priority: str = "medium"
    assignee_role: str | None = "planner"
    parent_id: int | None = None
    branch: str | None = "aiforge/ONE-99-test"
    project: str | None = "TestRepo"
    labels: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        pass


# ─────────────────────────── helpers ────────────────────────────────────

def _make_summary(stop_reason: str = "model_done") -> dict:
    return {
        "stop_reason": stop_reason,
        "has_commented": True,
        "turns": 3,
        "wall_s": 0.1,
    }


def _patch_node_internals(ticket: _FakeTicket):
    """Return a stack of patches that stubs out Postgres + LLM for all nodes."""
    def _fake_get(ident_or_id: Any) -> _FakeTicket | None:
        return ticket

    def _fake_add_event(*a, **kw) -> int:
        return 1

    def _fake_run_tool_loop(rc, t, worktree, log) -> dict:
        return _make_summary()

    def _fake_finalize(*a, **kw) -> None:
        return None

    def _fake_write_t1(*a, **kw) -> None:
        return None

    def _fake_ensure_worktree(t) -> str | None:
        return "/tmp/fake-worktree"

    def _fake_inject_context(state, role):
        return state

    patches = [
        patch("aiforge_core.graph.nodes.supervisor.tickets_mod.get", side_effect=_fake_get),
        patch("aiforge_core.graph.nodes.supervisor._run_tool_loop", side_effect=_fake_run_tool_loop),
        patch("aiforge_core.graph.nodes.supervisor._finalize_ticket", side_effect=_fake_finalize),
        patch("aiforge_core.graph.nodes.supervisor._ensure_branch_and_worktree", side_effect=_fake_ensure_worktree),
        patch("aiforge_core.graph.nodes.planner.tickets_mod.get", side_effect=_fake_get),
        patch("aiforge_core.graph.nodes.planner._run_tool_loop", side_effect=_fake_run_tool_loop),
        patch("aiforge_core.graph.nodes.planner._finalize_ticket", side_effect=_fake_finalize),
        patch("aiforge_core.graph.nodes.planner._ensure_branch_and_worktree", side_effect=_fake_ensure_worktree),
        patch("aiforge_core.graph.nodes.planner.inject_context", side_effect=_fake_inject_context),
        patch("aiforge_core.graph.nodes.doer.tickets_mod.get", side_effect=_fake_get),
        patch("aiforge_core.graph.nodes.doer._run_tool_loop", side_effect=_fake_run_tool_loop),
        patch("aiforge_core.graph.nodes.doer._finalize_ticket", side_effect=_fake_finalize),
        patch("aiforge_core.graph.nodes.doer._ensure_branch_and_worktree", side_effect=_fake_ensure_worktree),
        patch("aiforge_core.graph.nodes.doer.inject_context", side_effect=_fake_inject_context),
        patch("aiforge_core.graph.nodes.feedback.tickets_mod.get", side_effect=_fake_get),
        patch("aiforge_core.graph.nodes.feedback._run_tool_loop", side_effect=_fake_run_tool_loop),
        patch("aiforge_core.graph.nodes.feedback._finalize_ticket", side_effect=_fake_finalize),
        patch("aiforge_core.graph.nodes.feedback._write_t1_memory", side_effect=_fake_write_t1),
        patch("aiforge_core.graph.nodes.feedback._ensure_branch_and_worktree", side_effect=_fake_ensure_worktree),
        patch("aiforge_core.graph.nodes.learner.tickets_mod.get", side_effect=_fake_get),
        patch("aiforge_core.graph.nodes.learner._run_tool_loop", side_effect=_fake_run_tool_loop),
        patch("aiforge_core.graph.nodes.learner._finalize_ticket", side_effect=_fake_finalize),
        patch("aiforge_core.graph.nodes.learner._write_t1_memory", side_effect=_fake_write_t1),
        patch("aiforge_core.graph.nodes.learner._ensure_branch_and_worktree", side_effect=_fake_ensure_worktree),
    ]
    return patches


# ─────────────────────────── test ───────────────────────────────────────

class TestFullPipelineHappyPath:
    def test_full_pipeline_happy_path(self) -> None:
        """Supervisor→Planner→Doer→Feedback→Learner completes with stop_reason=done."""
        ticket = _FakeTicket()

        # Supervisor routes to planner (assignee_role=planner in ticket).
        # Feedback verdict=pass routes to learner.
        # Learner sets stop_reason=done.

        # We need feedback_node to return verdict=pass.
        # Override feedback get to return a ticket with metadata.
        def _fake_get_feedback(ident_or_id):
            t = _FakeTicket()
            t.metadata = {"feedback_verdict": "pass"}
            return t

        all_patches = _patch_node_internals(ticket)
        # Stack a feedback-specific override on top — ExitStack enters in order,
        # so the later patch wins for aiforge_core.graph.nodes.feedback.tickets_mod.get.
        all_patches.append(
            patch(
                "aiforge_core.graph.nodes.feedback.tickets_mod.get",
                side_effect=_fake_get_feedback,
            )
        )

        # Suppress checkpointer: empty DSN causes build_graph to skip it.
        with patch.dict("os.environ", {"AIFORGE_DSN": ""}):
            from aiforge_core.graph.graph import build_graph
            graph = build_graph()

        initial_state: AgentState = {
            "ticket_id": 1,
            "ticket": {
                "id": 1,
                "identifier": "ONE-99",
                "title": "Test ticket",
                "body": "",
                "status": "in_progress",
                "assignee_role": "planner",
                "parent_id": None,
                "branch": None,
                "project": None,
                "metadata": {},
            },
            "role": "supervisor",
            "messages": [],
            "tool_results": [],
            "worktree_path": None,
            "stop_reason": None,
            "compile_fail_count": 0,
            "verdict": None,
            "feedback_fixlist": None,
            "learner_digest": None,
            "flags": {
                "orchestrator.backend": "langgraph",
                "doer.backend": "legacy",
                "rag.backend": "legacy",
            },
        }

        import logging
        fake_log = logging.getLogger("aiforge.test")

        import contextlib
        ctx = contextlib.ExitStack()
        for p in all_patches:
            ctx.enter_context(p)
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.supervisor.get_logger", return_value=fake_log)
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.planner.get_logger", return_value=fake_log)
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.doer.get_logger", return_value=fake_log)
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.feedback.get_logger", return_value=fake_log)
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.learner.get_logger", return_value=fake_log)
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.supervisor.role_cfg_get",
                  return_value=MagicMock(name="supervisor"))
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.planner.role_cfg_get",
                  return_value=MagicMock(name="planner"))
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.doer.role_cfg_get",
                  return_value=MagicMock(name="doer"))
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.feedback.role_cfg_get",
                  return_value=MagicMock(name="feedback"))
        )
        ctx.enter_context(
            patch("aiforge_core.graph.nodes.learner.role_cfg_get",
                  return_value=MagicMock(name="learner"))
        )

        with ctx:
            result = graph.invoke(initial_state, config={"configurable": {"thread_id": "ONE-99"}})

        assert result["stop_reason"] == "done"
        assert len(result["tool_results"]) >= 1
