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

_BUILD_LOOP = 'Build loop'
_PLAN_VERIFY = 'Plan & verify'
_VALIDATE_FINISH = 'Validate & finish'
_CONTEXT_FAN_OUT_PARALLEL = 'Context fan-out (parallel)'

# Default lean graph (AIFORGE_FORCE_FULL_PIPELINE unset).
_LEAN_NODES: list[dict] = [
    {"id": "START", "label": "START", "type": "start", "stage": "Start",
     "desc": "Entry point — a ticket or chat request enters here."},
    {"id": "triage", "label": "Triage", "type": "agent", "stage": "Triage",
     "desc": "Routes the request: trivial → straight to Doer; else → Planner."},
    {"id": "triage_gate", "label": "Triage gate", "type": "gate", "stage": "Triage",
     "desc": "The trivial-vs-plan decision."},
    {"id": "planner", "label": "Planner", "type": "agent", "stage": _PLAN_VERIFY,
     "desc": "Splits the request into ordered, concrete subtasks."},
    {"id": "plan_promote", "label": "Plan promote", "type": "gate", "stage": _PLAN_VERIFY,
     "desc": "Promotes the plan into session state for the Doer."},
    {"id": "doer", "label": "Doer", "type": "agent", "stage": _BUILD_LOOP,
     "desc": "Writes the actual code + runs the tools for each subtask."},
    {"id": "feedback", "label": "Feedback", "type": "agent", "stage": _BUILD_LOOP,
     "desc": "In-loop reviewer: checks each pass and feeds corrections back."},
    {"id": "loop_gate", "label": "Loop gate", "type": "gate", "stage": _BUILD_LOOP,
     "desc": "Loop the Doer for another pass, or exit to validation."},
    {"id": "validator", "label": "Validator", "type": "agent", "stage": _VALIDATE_FINISH,
     "desc": "Final check of the built result against the plan."},
    {"id": "validator_gate", "label": "Validator gate", "type": "gate", "stage": _VALIDATE_FINISH,
     "desc": "Done, redo, or bounce back to re-plan."},
    {"id": "learner", "label": "Learner", "type": "agent", "stage": _VALIDATE_FINISH,
     "desc": "Persists durable lessons/memory so future runs start smarter."},
]

_LEAN_EDGES: list[dict] = [
    {"from": "START", "to": "triage", "label": ""},
    {"from": "triage", "to": "triage_gate", "label": ""},
    {"from": "triage_gate", "to": "doer", "label": "trivial"},
    {"from": "triage_gate", "to": "planner", "label": "small"},
    {"from": "planner", "to": "plan_promote", "label": ""},
    {"from": "plan_promote", "to": "doer", "label": ""},
    {"from": "doer", "to": "feedback", "label": ""},
    {"from": "feedback", "to": "loop_gate", "label": ""},
    {"from": "loop_gate", "to": "doer", "label": "loop"},
    {"from": "loop_gate", "to": "validator", "label": "exit"},
    {"from": "validator", "to": "validator_gate", "label": ""},
    {"from": "validator_gate", "to": "planner", "label": "replan"},
    {"from": "validator_gate", "to": "doer", "label": "redo"},
    {"from": "validator_gate", "to": "learner", "label": "done"},
]

# Full graph when AIFORGE_FORCE_FULL_PIPELINE=1.
_FULL_NODES: list[dict] = [
    {"id": "START", "label": "START", "type": "start", "stage": "Start",
     "desc": "Entry point — a ticket or chat request enters here."},
    {"id": "triage", "label": "Triage", "type": "agent", "stage": "Triage",
     "desc": "Routes the request: trivial → straight to Doer; substantial → full pipeline."},
    {"id": "triage_gate", "label": "Triage gate", "type": "gate", "stage": "Triage",
     "desc": "The trivial-vs-full decision."},
    {"id": "enhancer", "label": "Enhancer", "type": "agent", "stage": "Orchestrator",
     "desc": "Orchestrator: cleans the raw request into a clear spec + design."},
    {"id": "researcher", "label": "Researcher", "type": "branch", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Parallel branch: gathers the external/codebase context the plan needs."},
    {"id": "gap_eval", "label": "Gap critic", "type": "agent", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Checks whether research is complete; loops the Researcher until gaps close."},
    {"id": "gap_gate", "label": "Research-gap gate", "type": "gate", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Loop back for more research, or proceed."},
    {"id": "ctx_repomap", "label": "Ctx: repo map", "type": "branch", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Parallel branch: maps the repo structure for grounding."},
    {"id": "ctx_conventions", "label": "Ctx: conventions", "type": "branch", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Parallel branch: extracts the project's coding conventions."},
    {"id": "context_join", "label": "Context join", "type": "join", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Barrier: waits for all parallel context branches to finish."},
    {"id": "merge_context", "label": "Merge context", "type": "merge", "stage": _CONTEXT_FAN_OUT_PARALLEL,
     "desc": "Merges every branch's findings into one shared context."},
    {"id": "planner", "label": "Planner", "type": "agent", "stage": _PLAN_VERIFY,
     "desc": "Orchestrator: splits the design into ordered, concrete subtasks."},
    {"id": "verifier", "label": "Verifier (correctness+scope+risk)",
     "type": "agent", "stage": _PLAN_VERIFY,
     "desc": "Critiques the plan before any code — correctness + scope + risk in one call."},
    {"id": "verifier_gate", "label": "Verifier gate", "type": "gate", "stage": _PLAN_VERIFY,
     "desc": "Plan passes to the Doer, or bounces back to re-plan."},
    {"id": "doer", "label": "Doer", "type": "agent", "stage": _BUILD_LOOP,
     "desc": "Writes the actual code + runs the tools for each subtask."},
    {"id": "refiner", "label": "Refiner", "type": "agent", "stage": _BUILD_LOOP,
     "desc": "Polishes the Doer's output — cleanup, edge cases — inside the loop."},
    {"id": "feedback", "label": "Feedback", "type": "agent", "stage": _BUILD_LOOP,
     "desc": "In-loop reviewer: checks each pass and feeds corrections back."},
    {"id": "loop_gate", "label": "Loop gate", "type": "gate", "stage": _BUILD_LOOP,
     "desc": "Loop the Doer for another pass, or exit to validation."},
    {"id": "validator", "label": "Validator", "type": "agent", "stage": _VALIDATE_FINISH,
     "desc": "Final check of the built result against the plan."},
    {"id": "validator_gate", "label": "Validator gate", "type": "gate", "stage": _VALIDATE_FINISH,
     "desc": "Done, or bounce back to re-plan."},
    {"id": "learner", "label": "Learner", "type": "agent", "stage": _VALIDATE_FINISH,
     "desc": "Persists durable lessons/memory so future runs start smarter."},
]

_FULL_EDGES: list[dict] = [
    {"from": "START", "to": "triage", "label": ""},
    {"from": "triage", "to": "triage_gate", "label": ""},
    {"from": "triage_gate", "to": "doer", "label": "trivial"},
    {"from": "triage_gate", "to": "planner", "label": "small"},
    {"from": "triage_gate", "to": "enhancer", "label": "full"},
    {"from": "enhancer", "to": "researcher", "label": ""},
    {"from": "enhancer", "to": "ctx_repomap", "label": ""},
    {"from": "enhancer", "to": "ctx_conventions", "label": ""},
    {"from": "researcher", "to": "gap_eval", "label": ""},
    {"from": "gap_eval", "to": "gap_gate", "label": ""},
    {"from": "gap_gate", "to": "researcher", "label": "gap"},
    {"from": "gap_gate", "to": "context_join", "label": "ok"},
    {"from": "ctx_repomap", "to": "context_join", "label": ""},
    {"from": "ctx_conventions", "to": "context_join", "label": ""},
    {"from": "context_join", "to": "merge_context", "label": ""},
    {"from": "merge_context", "to": "planner", "label": ""},
    {"from": "planner", "to": "verifier", "label": ""},
    {"from": "verifier", "to": "verifier_gate", "label": ""},
    {"from": "verifier_gate", "to": "doer", "label": "pass"},
    {"from": "verifier_gate", "to": "planner", "label": "replan"},
    {"from": "doer", "to": "refiner", "label": ""},
    {"from": "refiner", "to": "feedback", "label": ""},
    {"from": "feedback", "to": "loop_gate", "label": ""},
    {"from": "loop_gate", "to": "doer", "label": "loop"},
    {"from": "loop_gate", "to": "validator", "label": "exit"},
    {"from": "validator", "to": "validator_gate", "label": ""},
    {"from": "validator_gate", "to": "planner", "label": "replan"},
    {"from": "validator_gate", "to": "doer", "label": "redo"},
    {"from": "validator_gate", "to": "learner", "label": "done"},
]

_LEAN_NODE_ROLE = {
    "triage": "triage", "planner": "planner", "doer": "doer",
    "feedback": "feedback", "validator": "verifier", "learner": "learner",
}

_FULL_NODE_ROLE = {
    "triage": "triage", "enhancer": "architect", "researcher": "researcher",
    "gap_eval": "gap_eval", "ctx_repomap": "ctx_repomap",
    "ctx_conventions": "ctx_conventions", "planner": "planner",
    "verifier": "verifier", "validator": "verifier",
    "doer": "doer", "refiner": "refiner",
    "feedback": "feedback", "learner": "learner",
}

_CONTEXT_CONSUMERS = ("enhancer", "planner", "doer")


def _active_topology() -> tuple[list[dict], list[dict], dict]:
    try:
        from aiforge_core.runtime.pipeline import lean_pipeline_enabled
        if lean_pipeline_enabled():
            return _LEAN_NODES, _LEAN_EDGES, _LEAN_NODE_ROLE
    except Exception:  # noqa: BLE001
        pass
    return _FULL_NODES, _FULL_EDGES, _FULL_NODE_ROLE


# Back-compat aliases used by older tests / imports.
def _NODES():  # noqa: N802 — historical name
    return _active_topology()[0]


def _EDGES():  # noqa: N802
    return _active_topology()[1]


def _NODE_ROLE():  # noqa: N802
    return _active_topology()[2]


# Module-level names expected as lists/dicts by existing callers — refreshed
# on each snapshot so FORCE_FULL flips show in the UI without a restart of
# the import. snapshot() always calls _active_topology() directly.
_NODES = _LEAN_NODES
_EDGES = _LEAN_EDGES
_NODE_ROLE = _LEAN_NODE_ROLE


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
    nodes, _edges, role_map = _active_topology()
    out = []
    for n in nodes:
        role = role_map.get(n["id"])
        out.append({**n, "tools": _tools_for(role) if role else [],
                    "status": "idle", "last_event_at": None,
                    "skills": [], "rules": [], "workflows": []})
    return out


def _ticket_events(ticket: str) -> list:
    try:
        from aiforge_core.tickets import store as _store
        t = _store.get(ticket)
        return _store.comments(t.id, 1000) if t else []
    except Exception:  # noqa: BLE001
        return []


def _absorb_injected(meta: dict, context: dict, seen: dict) -> None:
    """Accumulate context_injected metadata, de-duped by name — a replan
    re-injects the same skills, and each should show once."""
    for bucket in ("skills", "rules", "workflows"):
        for item in (meta.get(bucket) or []):
            name = item.get("name") if isinstance(item, dict) else None
            if name and name not in seen[bucket]:
                seen[bucket].add(name)
                context[bucket].append(item)


def _event_times(events: list, context: dict) -> dict:
    """``{role or stage: latest event time}``; fills ``context`` on the way."""
    last: dict[str, str] = {}
    seen: dict[str, set] = {"skills": set(), "rules": set(), "workflows": set()}
    for e in events:
        meta = e.get("metadata") or {}
        if (e.get("kind") or "") == "context_injected":
            _absorb_injected(meta, context, seen)
            continue
        role = e.get("agent_role") or ""
        ts = e.get("created_at")
        ts = ts.isoformat() if hasattr(ts, "isoformat") else ts
        for key in {role, meta.get("stage") or role}:
            if key:
                last[key] = ts
    return last


def _overlay_ticket(nodes: list[dict], ticket: str, role_map: dict) -> dict:
    """Mark nodes done/active from the ticket's events AND attach the skills /
    rules / workflows the run injected. Returns a run-level ``context`` summary
    (``{skills, rules, workflows}``) the UI renders as a legend."""
    context: dict[str, list] = {"skills": [], "rules": [], "workflows": []}
    last = _event_times(_ticket_events(ticket), context)
    for n in nodes:
        role = role_map.get(n["id"])
        hit = last.get(n["id"]) or (last.get(role) if role else None)
        if hit:
            n["status"] = "done"
            n["last_event_at"] = hit
        if n["id"] in _CONTEXT_CONSUMERS:
            n["skills"] = context["skills"]
            n["workflows"] = context["workflows"]
            n["rules"] = context["rules"]
    return context


def snapshot(ticket: "str | None" = None) -> dict:
    nodes_src, edges, role_map = _active_topology()
    global _NODES, _EDGES, _NODE_ROLE
    _NODES, _EDGES, _NODE_ROLE = nodes_src, edges, role_map
    nodes = _base_nodes()
    context: dict[str, list] = {"skills": [], "rules": [], "workflows": []}
    if ticket:
        context = _overlay_ticket(nodes, ticket, role_map)
    return {"nodes": nodes, "edges": list(edges), "ticket": ticket,
            "context": context, "lean": lean_pipeline_enabled_safe()}


def lean_pipeline_enabled_safe() -> bool:
    try:
        from aiforge_core.runtime.pipeline import lean_pipeline_enabled
        return lean_pipeline_enabled()
    except Exception:  # noqa: BLE001
        return True
