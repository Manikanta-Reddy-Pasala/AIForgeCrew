from __future__ import annotations

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import role as role_cfg_get
from aiforge_core.runtime.logging_setup import get_logger
from aiforge_core.runtime.orchestrator import (
    _ensure_branch_and_worktree,
    _finalize_ticket,
    _run_tool_loop,
)

from ..state import AgentState


def supervisor_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    rc = role_cfg_get("supervisor")
    log = get_logger("supervisor")
    worktree = state.get("worktree_path") or _ensure_branch_and_worktree(ticket)

    summary = _run_tool_loop(rc, ticket, worktree, log)
    _finalize_ticket(ticket, "supervisor", summary, log)

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    return {
        **state,
        "role": "supervisor",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": summary.get("stop_reason"),
        "tool_results": state.get("tool_results", []) + [summary],
    }
