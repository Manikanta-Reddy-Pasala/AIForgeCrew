"""Bridge between the graph node and the smolagents Planner agent.

``run_planner`` is called from aiforge_core.legacy.graph.nodes.planner.planner_node.
Unlike the Doer, the Planner does not clone a worktree — it reads across
WORKTREE_ROOT in place.
"""
from __future__ import annotations

import time

from aiforge_core.runtime import tickets
from aiforge_core.runtime.config import (
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    PLANNER_MODEL,
)
from aiforge_core.runtime.logging_setup import emit

from .agent import build_planner_agent


class _LLMConfig:
    """Minimal config shim — mirrors the one in doer/orchestrator_bridge.py."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key


def run_planner(ticket: object, log: object) -> dict:
    """Run the smolagents ToolCallingAgent for one Planner tick.

    The Planner does not need a git worktree — it only reads files.
    After the agent finishes, this function fetches the latest ticket body
    from Postgres (write_plan may have updated it) and returns it in the
    result dict so the graph node can refresh state.

    Returns a dict shaped like run_smolagents_doer:
        stop_reason, has_commented, turns, wall_s, summary, enriched_ticket_body
    """
    t_start = time.time()
    ticket_id = ticket.id  # type: ignore[attr-defined]

    # UnifiedContext — single source of context for ALL agents. Keyed
    # off ticket text so even raw plain-language tickets get the
    # focal_files / similar_tickets / T3 recipes / repo standards
    # they need before deciding scope.
    try:
        from aiforge_core.context import UnifiedContext as _UC
        _bundle = _UC().for_planner(ticket, token_budget=4000)
        context_bundle = _bundle.render() or (
            f"Project: {getattr(ticket, 'project', None) or 'unknown'}\n"
            f"Title: {getattr(ticket, 'title', '')}\n"
        )
    except Exception:
        context_bundle = (
            f"Project: {getattr(ticket, 'project', None) or 'unknown'}\n"
            f"Title: {getattr(ticket, 'title', '')}\n"
        )

    llm_config = _LLMConfig(
        base_url=LM_STUDIO_BASE_URL,
        model=PLANNER_MODEL,
        api_key=LM_STUDIO_API_KEY,
    )

    emit(log, "planner.smolagents.start",
         ticket=getattr(ticket, "identifier", "?"))

    try:
        agent, task_prompt = build_planner_agent(ticket, context_bundle, llm_config)
        result = agent.run(task=task_prompt)
        summary_text = str(result) if result is not None else ""

        # Record a comment on the ticket so the event stream shows planner output.
        tickets.add_event(
            ticket_id, "planner", "comment",
            body=summary_text[:4000],
            metadata={"source": "planner_smolagents"},
        )

        # Fetch refreshed body (write_plan may have mutated it in Postgres).
        fresh = tickets.get(ticket_id)
        enriched_body = (fresh.body if fresh else getattr(ticket, "body", "")) or ""

        emit(log, "planner.smolagents.done",
             ticket=getattr(ticket, "identifier", "?"),
             summary_chars=len(summary_text))

        return {
            "stop_reason": "final_answer",
            "has_commented": bool(summary_text),
            "turns": getattr(agent, "step_number", 0),
            "wall_s": round(time.time() - t_start, 2),
            "summary": summary_text,
            "enriched_ticket_body": enriched_body,
        }

    except Exception as exc:
        emit(log, "planner.smolagents.exception",
             ticket=getattr(ticket, "identifier", "?"),
             error=str(exc)[:300])
        tickets.add_event(
            ticket_id, "planner", "error",
            body=f"planner smolagents exception: {exc}",
            metadata={"stop_reason": "exception"},
        )
        return {
            "stop_reason": "exception",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
            "enriched_ticket_body": getattr(ticket, "body", "") or "",
        }
