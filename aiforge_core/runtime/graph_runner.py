from __future__ import annotations

import time

from aiforge_core.graph import build_graph
from aiforge_core.graph.state import AgentState
from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.feature_flags import get_flag
from aiforge_core.runtime.logging_setup import emit, get_logger


def run_graph(ticket_id: int) -> int:
    log = get_logger("graph_runner")
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        emit(log, "graph_runner.not_found", ticket_id=ticket_id)
        return 1

    flags: dict = {
        "orchestrator.backend": get_flag("orchestrator.backend", "legacy"),
        "doer.backend": get_flag("doer.backend", "legacy"),
        "rag.backend": get_flag("rag.backend", "legacy"),
    }

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
        "verdict": None,
        "feedback_fixlist": None,
        "learner_digest": None,
        "flags": flags,
    }

    graph = build_graph()
    config = {"configurable": {"thread_id": ticket.identifier}}

    t_start = time.time()
    emit(log, "graph_runner.start", ticket=ticket.identifier, title=ticket.title)

    try:
        final_state = graph.invoke(state, config=config)
        wall_s = round(time.time() - t_start, 2)
        emit(
            log,
            "graph_runner.done",
            ticket=ticket.identifier,
            stop_reason=final_state.get("stop_reason"),
            verdict=final_state.get("verdict"),
            wall_s=wall_s,
        )
        tickets_mod.add_event(
            ticket_id,
            "graph_runner",
            "comment",
            body=f"graph run complete stop_reason={final_state.get('stop_reason')} "
                 f"verdict={final_state.get('verdict')} wall_s={wall_s}",
            metadata={"wall_s": wall_s, "flags": flags},
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
