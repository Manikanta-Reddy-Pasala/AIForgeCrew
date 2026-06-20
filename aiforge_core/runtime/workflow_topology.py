"""Real v6 pipeline topology for the Workflow UI.

Mirrors the actual ADK 2.x ``google.adk.workflow.Workflow`` graph wired
in :mod:`aiforge_core.runtime.pipeline` (see its module docstring). We
encode the graph as data here — rather than building the live Workflow
(which instantiates every LlmAgent) — so the UI endpoint stays cheap and
can't fail on model construction. The shape is kept in lockstep with
``pipeline.build_pipeline``'s edges.

``snapshot(ticket)`` returns ``{nodes, edges, ticket}``. When a ticket
identifier is given, each node is overlaid with a ``status`` +
``last_event_at`` derived from that ticket's ``ticket_events`` (which
agent_role fired, and when).
"""
from __future__ import annotations

# Node kinds: agent (LLM stage) · gate (conditional route) · branch
# (parallel fan-out member) · join (parallel barrier) · merge (state
# merge) · start.
_NODES: list[dict] = [
    {"id": "START", "label": "START", "type": "start"},
    {"id": "triage", "label": "Triage", "type": "agent"},
    {"id": "triage_gate", "label": "Triage gate", "type": "gate"},
    {"id": "enhancer", "label": "Enhancer", "type": "agent"},
    {"id": "researcher", "label": "Researcher", "type": "branch"},
    {"id": "gap_eval", "label": "Gap critic", "type": "agent"},
    {"id": "gap_gate", "label": "Research-gap gate", "type": "gate"},
    {"id": "ctx_repomap", "label": "Ctx: repo map", "type": "branch"},
    {"id": "ctx_conventions", "label": "Ctx: conventions", "type": "branch"},
    {"id": "context_join", "label": "Context join", "type": "join"},
    {"id": "merge_context", "label": "Merge context", "type": "merge"},
    {"id": "planner", "label": "Planner", "type": "agent"},
    {"id": "verify_correctness", "label": "Verify: correctness", "type": "branch"},
    {"id": "verify_scope", "label": "Verify: scope", "type": "branch"},
    {"id": "verify_risk", "label": "Verify: risk", "type": "branch"},
    {"id": "verifier_join", "label": "Verifier join", "type": "join"},
    {"id": "merge_verdicts", "label": "Merge verdicts", "type": "merge"},
    {"id": "verifier_gate", "label": "Verifier gate", "type": "gate"},
    {"id": "doer", "label": "Doer", "type": "agent"},
    {"id": "refiner", "label": "Refiner", "type": "agent"},
    {"id": "feedback", "label": "Feedback", "type": "agent"},
    {"id": "loop_gate", "label": "Loop gate", "type": "gate"},
    {"id": "validator", "label": "Validator", "type": "agent"},
    {"id": "validator_gate", "label": "Validator gate", "type": "gate"},
    {"id": "learner", "label": "Learner", "type": "agent"},
]

_EDGES: list[dict] = [
    {"from": "START", "to": "triage", "label": ""},
    {"from": "triage", "to": "triage_gate", "label": ""},
    {"from": "triage_gate", "to": "doer", "label": "trivial"},
    {"from": "triage_gate", "to": "enhancer", "label": "full"},
    # parallel context fan-out
    {"from": "enhancer", "to": "researcher", "label": ""},
    {"from": "enhancer", "to": "ctx_repomap", "label": ""},
    {"from": "enhancer", "to": "ctx_conventions", "label": ""},
    # research-gap loop
    {"from": "researcher", "to": "gap_eval", "label": ""},
    {"from": "gap_eval", "to": "gap_gate", "label": ""},
    {"from": "gap_gate", "to": "researcher", "label": "gap"},
    {"from": "gap_gate", "to": "context_join", "label": "ok"},
    {"from": "ctx_repomap", "to": "context_join", "label": ""},
    {"from": "ctx_conventions", "to": "context_join", "label": ""},
    {"from": "context_join", "to": "merge_context", "label": ""},
    {"from": "merge_context", "to": "planner", "label": ""},
    # parallel verifier fan-out
    {"from": "planner", "to": "verify_correctness", "label": ""},
    {"from": "planner", "to": "verify_scope", "label": ""},
    {"from": "planner", "to": "verify_risk", "label": ""},
    {"from": "verify_correctness", "to": "verifier_join", "label": ""},
    {"from": "verify_scope", "to": "verifier_join", "label": ""},
    {"from": "verify_risk", "to": "verifier_join", "label": ""},
    {"from": "verifier_join", "to": "merge_verdicts", "label": ""},
    {"from": "merge_verdicts", "to": "verifier_gate", "label": ""},
    {"from": "verifier_gate", "to": "doer", "label": "pass"},
    {"from": "verifier_gate", "to": "planner", "label": "replan"},
    # doer loop
    {"from": "doer", "to": "refiner", "label": ""},
    {"from": "refiner", "to": "feedback", "label": ""},
    {"from": "feedback", "to": "loop_gate", "label": ""},
    {"from": "loop_gate", "to": "doer", "label": "loop"},
    {"from": "loop_gate", "to": "validator", "label": "exit"},
    # validate + replan/done
    {"from": "validator", "to": "validator_gate", "label": ""},
    {"from": "validator_gate", "to": "planner", "label": "replan"},
    {"from": "validator_gate", "to": "learner", "label": "done"},
]

# Map graph node id -> the agent role whose tools it runs (for the UI's
# "N tools" badge). Non-agent nodes (gates/joins/merges) carry no tools.
_NODE_ROLE = {
    "triage": "triage", "enhancer": "architect", "researcher": "researcher",
    "gap_eval": "gap_eval", "ctx_repomap": "ctx_repomap",
    "ctx_conventions": "ctx_conventions", "planner": "planner",
    "verify_correctness": "verify_correctness", "verify_scope": "verify_scope",
    "verify_risk": "verify_risk", "doer": "doer", "refiner": "refiner",
    "feedback": "feedback", "validator": "verifier", "learner": "learner",
}


def _tools_for(role: str) -> list[str]:
    try:
        from aiforge_core.config.env import ROLES  # type: ignore
        rc = ROLES.get(role)
        if rc is not None and getattr(rc, "tool_allowlist", None):
            return list(rc.tool_allowlist)
    except Exception:
        pass
    return []


def _base_nodes() -> list[dict]:
    out = []
    for n in _NODES:
        role = _NODE_ROLE.get(n["id"])
        out.append({**n, "tools": _tools_for(role) if role else [],
                    "status": "idle", "last_event_at": None})
    return out


def _overlay_ticket(nodes: list[dict], ticket: str) -> None:
    """Mark nodes done/active from the ticket's events."""
    try:
        from aiforge_core.tickets import store as _store
        t = _store.get(ticket)
        if not t:
            return
        events = _store.comments(t.id, 1000)
    except Exception:
        return
    # role/stage -> latest event time
    last: dict[str, str] = {}
    for e in events:
        role = e.get("agent_role") or ""
        meta = e.get("metadata") or {}
        stage = meta.get("stage") or role
        ts = e.get("created_at")
        ts = ts.isoformat() if hasattr(ts, "isoformat") else ts
        for key in {role, stage}:
            if key:
                last[key] = ts
    for n in nodes:
        role = _NODE_ROLE.get(n["id"])
        hit = last.get(n["id"]) or (last.get(role) if role else None)
        if hit:
            n["status"] = "done"
            n["last_event_at"] = hit


def snapshot(ticket: "str | None" = None) -> dict:
    nodes = _base_nodes()
    if ticket:
        _overlay_ticket(nodes, ticket)
    return {"nodes": nodes, "edges": list(_EDGES), "ticket": ticket}
