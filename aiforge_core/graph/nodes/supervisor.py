"""Supervisor node — lightweight routing only.

Previous legacy path ran _run_tool_loop which blew context on qwen-coder/
gemma-12b and looped on tool-call grammar. Supervisor is routing-only —
it reads the ticket, ensures a worktree, and forwards state to the
downstream node picked by `route_from_supervisor` based on assignee_role.
"""
from __future__ import annotations

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import emit, get_logger
from aiforge_core.runtime.orchestrator import _ensure_branch_and_worktree

from ..state import AgentState


def supervisor_node(state: AgentState) -> AgentState:
    ticket_id = state["ticket_id"]
    ticket = tickets_mod.get(ticket_id)
    if ticket is None:
        return {**state, "stop_reason": "blocked"}

    log = get_logger("supervisor")
    worktree = state.get("worktree_path") or _ensure_branch_and_worktree(ticket)

    assignee = ticket.assignee_role or "planner"
    emit(log, "supervisor.route", ticket=ticket.identifier, assignee=assignee)

    updated_ticket = {
        "id": ticket.id,
        "identifier": ticket.identifier,
        "title": ticket.title,
        "body": ticket.body,
        "status": ticket.status,
        "assignee_role": assignee,
        "parent_id": ticket.parent_id,
        "branch": ticket.branch,
        "project": ticket.project,
        "metadata": ticket.metadata or {},
    }

    return {
        **state,
        "role": "supervisor",
        "ticket": updated_ticket,
        "worktree_path": worktree,
        "stop_reason": None,
    }
