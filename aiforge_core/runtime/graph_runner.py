from __future__ import annotations

import time

from aiforge_core.graph import build_graph
from aiforge_core.graph.state import AgentState
from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import emit, get_logger


def run_graph(ticket_id: int) -> int:
    log = get_logger("graph_runner")
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        emit(log, "graph_runner.not_found", ticket_id=ticket_id)
        return 1

    state: AgentState = {
        "ticket_id": ticket_id,
        "ticket": {
            "id": ticket.id,
            "identifier": ticket.identifier,
            "title": ticket.title,
            "body": ticket.body,
            "status": ticket.status,
            "assignee_role": ticket.assignee_role,
            "parent_id": ticket.parent_id,
            "branch": ticket.branch,
            "project": ticket.project,
            "metadata": ticket.metadata or {},
        },
        "role": "supervisor",
        "messages": [],
        "tool_results": [],
        "worktree_path": None,
        "stop_reason": None,
        "compile_fail_count": 0,
        "feedback_fail_count": 0,
        "verdict": None,
        "feedback_fixlist": None,
        "learner_digest": None,
        "flags": {},
    }

    graph = build_graph()
    config = {"configurable": {"thread_id": ticket.identifier}}

    t_start = time.time()
    emit(log, "graph_runner.start", ticket=ticket.identifier, title=ticket.title)

    try:
        final_state = graph.invoke(state, config=config)
        wall_s = round(time.time() - t_start, 2)
        stop_reason = final_state.get("stop_reason") or ""
        verdict = final_state.get("verdict") or ""

        # Map graph terminal state → ticket status. The graph END edge
        # leaves the ticket at whatever status _finalize_ticket wrote
        # from the last node; we override here so reclaim doesn't
        # immediately pull the ticket again.
        if stop_reason == "done" or verdict == "pass":
            new_status = "done"
        elif verdict == "scope_violation":
            new_status = "blocked"
        elif stop_reason in ("blocked", "loop_detected"):
            new_status = "blocked"
        else:
            new_status = None  # let reclaim logic handle transient failures

        if new_status:
            tickets_mod.update_status(ticket_id, new_status)

        emit(
            log,
            "graph_runner.done",
            ticket=ticket.identifier,
            stop_reason=stop_reason,
            verdict=verdict,
            final_status=new_status,
            wall_s=wall_s,
        )
        tickets_mod.add_event(
            ticket_id,
            "graph_runner",
            "comment",
            body=f"graph run complete stop_reason={stop_reason} "
                 f"verdict={verdict} wall_s={wall_s} status={new_status}",
            metadata={"wall_s": wall_s, "final_status": new_status},
        )
        return 0
    except Exception as exc:
        emit(log, "graph_runner.exception", ticket=ticket.identifier,
             error=str(exc)[:500])
        tickets_mod.add_event(
            ticket_id, "graph_runner", "error",
            body=f"graph runner exception: {exc}",
        )
        return 2
