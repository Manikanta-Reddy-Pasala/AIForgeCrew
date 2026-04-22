from __future__ import annotations

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import role as role_cfg_get
from aiforge_core.runtime.logging_setup import get_logger
from aiforge_core.runtime.orchestrator import (
    _ensure_branch_and_worktree,
    _run_tool_loop,
)

from ..state import AgentState
from .retriever import inject_context


def planner_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    rc = role_cfg_get("planner")
    log = get_logger("planner")
    worktree = state.get("worktree_path") or _ensure_branch_and_worktree(ticket)

    updated_state = inject_context(state, "planner")

    summary = _run_tool_loop(rc, ticket, worktree, log)
    # Graph-runner sets terminal status at END.

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    return {
        **updated_state,
        "role": "planner",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": summary.get("stop_reason"),
        "tool_results": state.get("tool_results", []) + [summary],
    }
