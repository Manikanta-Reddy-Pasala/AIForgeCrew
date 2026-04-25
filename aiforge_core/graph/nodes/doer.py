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
        # Forward prior feedback so Doer can continue from the last tick's state
        # instead of re-planning from scratch (2026-04-23). Also surface the
        # last compile error from the prior Doer tick (stashed by run_compile
        # tool in counters['last_compile_error']) so the model sees the exact
        # error to fix instead of guessing.
        prior_fixlist = state.get("feedback_fixlist") or ""
        last_compile_err = ""
        try:
            evs = tickets_mod.comments(ticket_id, limit=50)
            for ev in reversed(evs):
                if ev.get("kind") == "error":
                    md = ev.get("metadata") or {}
                    if md.get("stop_reason") == "checklist_fail":
                        last_compile_err = md.get("compile_error") or ""
                        if last_compile_err:
                            break
        except Exception:
            pass
        if last_compile_err:
            prior_fixlist = (
                f"COMPILE FAILED on previous tick — first fix this:\n"
                f"```\n{last_compile_err}\n```\n\n"
                + (prior_fixlist if prior_fixlist else "")
            )
        summary = run_smolagents_doer(
            ticket, worktree, log,
            prior_verdict=state.get("verdict") or ("fail" if last_compile_err else None),
            prior_fixlist=prior_fixlist or None,
        )
    else:
        summary = _run_tool_loop(rc, ticket, worktree, log)

    # Graph-runner handles terminal status at END; don't call _finalize_ticket
    # here — it treats stop_reason='final_answer' as unknown and blocks the
    # ticket, short-circuiting the doer→feedback→learner flow.

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
