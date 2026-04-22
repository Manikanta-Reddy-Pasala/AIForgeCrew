from __future__ import annotations

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import get_logger

from ..state import AgentState
from .retriever import inject_context


def planner_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    log = get_logger("planner")

    updated_state = inject_context(state, "planner")

    # Lazy import avoids circular imports at module load time.
    from aiforge_core.planner import run_planner

    summary = run_planner(ticket, log)
    # Graph-runner sets terminal status at END.

    # Refresh ticket from Postgres — run_planner/write_plan may have updated body.
    fresh = tickets_mod.get(ticket_id)
    updated_ticket = dict(fresh.__dict__) if fresh else state["ticket"]

    return {
        **updated_state,
        "role": "planner",
        "ticket": updated_ticket,
        "stop_reason": summary.get("stop_reason"),
        "tool_results": state.get("tool_results", []) + [summary],
    }
