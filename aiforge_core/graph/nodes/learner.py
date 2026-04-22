from __future__ import annotations

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import role as role_cfg_get
from aiforge_core.runtime.logging_setup import get_logger
from aiforge_core.runtime.orchestrator import (
    _ensure_branch_and_worktree,
    _finalize_ticket,
    _run_tool_loop,
    _write_t1_memory,
)

from ..state import AgentState


def learner_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    rc = role_cfg_get("learner")
    log = get_logger("learner")
    worktree = state.get("worktree_path") or _ensure_branch_and_worktree(ticket)

    summary = _run_tool_loop(rc, ticket, worktree, log)
    _finalize_ticket(ticket, "learner", summary, log)

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    _write_t1_memory(ticket, "learner", summary, log)

    learner_digest = (updated_ticket.get("body") or "")[:2000]

    return {
        **state,
        "role": "learner",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": "done",
        "learner_digest": learner_digest,
        "tool_results": state.get("tool_results", []) + [summary],
    }
