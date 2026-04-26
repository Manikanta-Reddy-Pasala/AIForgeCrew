"""Workflow DAG snapshot — feeds the Web UI graph view.

KISS: a static topology spec representing the live AIForge sequence
``Architect → Planner → Doer ⇄ Feedback → Integration → Publish →
Learner``, plus an optional per-ticket overlay decorating each node
with the most recent status pulled from ``ticket_events``.

The topology IS hardcoded today — when ADK 2.0 ``Workflow(BaseNode)``
lands we'll derive the graph from ``Workflow._workflow_graph``
instead. Interface (``snapshot``) stays identical so the UI doesn't
care.

Public surface:
- ``snapshot(ticket: str | None = None) -> dict``
"""
from __future__ import annotations


_NODES = [
    {"id": "architect",   "label": "Architect",   "type": "agent", "tools": ["external"]},
    {"id": "planner",     "label": "Planner",     "type": "agent", "tools": ["lookup_repo", "search_memory", "grep_repos", "read_file", "write_plan"]},
    {"id": "doer",        "label": "Doer",        "type": "agent", "tools": ["file_patch", "bulk_edit", "code_run", "ask_explorer", "todo_write", "plan_mode"]},
    {"id": "feedback",    "label": "Feedback",    "type": "agent", "tools": ["targeted_fixlist"]},
    {"id": "integration", "label": "Integration", "type": "tool",  "tools": ["spring_boot_runner"]},
    {"id": "publish",     "label": "Publish",     "type": "tool",  "tools": ["github_pr"]},
    {"id": "learner",     "label": "Learner",     "type": "agent", "tools": ["distill", "retain_fact"]},
]

_EDGES = [
    {"from": "architect",   "to": "planner",     "label": "ticket"},
    {"from": "planner",     "to": "doer",        "label": "plan"},
    {"from": "doer",        "to": "feedback",    "label": "edits"},
    {"from": "feedback",    "to": "doer",        "label": "fixlist (loop)"},
    {"from": "feedback",    "to": "integration", "label": "compile-green"},
    {"from": "integration", "to": "publish",     "label": "smoke-ok"},
    {"from": "publish",     "to": "learner",     "label": "PR-ready"},
]


def snapshot(ticket: str | None = None) -> dict:
    """Return the topology + (optional) per-ticket overlay."""
    nodes = [dict(n) for n in _NODES]
    if ticket:
        try:
            overlay = _ticket_overlay(ticket)
        except Exception:
            overlay = {}
        for n in nodes:
            n["status"] = overlay.get(n["id"], "idle")
            n["last_event_at"] = overlay.get(f"{n['id']}_last", None)
    return {
        "nodes": nodes,
        "edges": _EDGES,
        "ticket": ticket,
    }


# ───────── helpers ────────────────────────────────────────────────


def _ticket_overlay(ticket: str) -> dict:
    """Latest event-kind per agent role for a ticket. Best-effort."""
    import psycopg
    from .config import AIFORGE_DSN
    out: dict = {}
    sql = (
        "SELECT agent_role, MAX(created_at) AS last_at, "
        "       (array_agg(kind ORDER BY created_at DESC))[1] AS last_kind "
        "FROM ticket_events "
        "WHERE ticket_id IN ("
        "  SELECT id FROM tickets WHERE identifier = %s LIMIT 1) "
        "GROUP BY agent_role"
    )
    with psycopg.connect(AIFORGE_DSN, connect_timeout=2,
                         options="-c statement_timeout=3000") as c, \
         c.cursor() as cur:
        cur.execute(sql, (ticket,))
        for role, last_at, last_kind in cur.fetchall():
            out[role] = last_kind or "active"
            out[f"{role}_last"] = last_at.isoformat() if last_at else None
    return out
