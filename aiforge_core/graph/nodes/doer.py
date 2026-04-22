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
from .retriever import inject_context


def doer_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    rc = role_cfg_get("doer")
    log = get_logger("doer")
    worktree = state.get("worktree_path") or _ensure_branch_and_worktree(ticket)

    updated_state = inject_context(state, "doer")

    if worktree is not None:
        from aiforge_core.doer import run_smolagents_doer
        summary = run_smolagents_doer(ticket, worktree, log)
    else:
        summary = _run_tool_loop(rc, ticket, worktree, log)

    _finalize_ticket(ticket, "doer", summary, log)

    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    compile_fail_count = state.get("compile_fail_count") or 0
    stop = summary.get("stop_reason", "")
    if "compile" in stop or stop == "scope_violation":
        compile_fail_count += 1

    return {
        **updated_state,
        "role": "doer",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": summary.get("stop_reason"),
        "compile_fail_count": compile_fail_count,
        "tool_results": state.get("tool_results", []) + [summary],
    }
