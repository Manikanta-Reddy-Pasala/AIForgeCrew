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
# ``stage`` groups nodes into the pipeline layer they belong to (shown as a
# band/legend in the UI). ``desc`` = one-line "what it does" (UI tooltip).
_NODES: list[dict] = [
    {"id": "START", "label": "START", "type": "start", "stage": "Start",
     "desc": "Entry point — a ticket or chat request enters here."},
    {"id": "triage", "label": "Triage", "type": "agent", "stage": "Triage",
     "desc": "Routes the request: trivial → straight to Doer; substantial → full pipeline."},
    {"id": "triage_gate", "label": "Triage gate", "type": "gate", "stage": "Triage",
     "desc": "The trivial-vs-full decision."},
    {"id": "enhancer", "label": "Enhancer", "type": "agent", "stage": "Orchestrator",
     "desc": "Orchestrator: cleans the raw request into a clear spec + design (runs the architect role)."},
    {"id": "researcher", "label": "Researcher", "type": "branch", "stage": "Context fan-out (parallel)",
     "desc": "Parallel branch: gathers the external/codebase context the plan needs."},
    {"id": "gap_eval", "label": "Gap critic", "type": "agent", "stage": "Context fan-out (parallel)",
     "desc": "Checks whether research is complete; loops the Researcher until gaps close."},
    {"id": "gap_gate", "label": "Research-gap gate", "type": "gate", "stage": "Context fan-out (parallel)",
     "desc": "Loop back for more research, or proceed."},
    {"id": "ctx_repomap", "label": "Ctx: repo map", "type": "branch", "stage": "Context fan-out (parallel)",
     "desc": "Parallel branch: maps the repo structure for grounding."},
    {"id": "ctx_conventions", "label": "Ctx: conventions", "type": "branch", "stage": "Context fan-out (parallel)",
     "desc": "Parallel branch: extracts the project's coding conventions (skipped when repo rules exist)."},
    {"id": "context_join", "label": "Context join", "type": "join", "stage": "Context fan-out (parallel)",
     "desc": "Barrier: waits for all parallel context branches to finish."},
    {"id": "merge_context", "label": "Merge context", "type": "merge", "stage": "Context fan-out (parallel)",
     "desc": "Merges every branch's findings into one shared context."},
    {"id": "planner", "label": "Planner", "type": "agent", "stage": "Plan & verify",
     "desc": "Orchestrator: splits the design into ordered, concrete subtasks."},
    {"id": "verifier", "label": "Verifier (correctness+scope+risk)",
     "type": "agent", "stage": "Plan & verify",
     "desc": "Critiques the plan before any code — correctness + scope + risk in one multi-axis call."},
    {"id": "verifier_gate", "label": "Verifier gate", "type": "gate", "stage": "Plan & verify",
     "desc": "Plan passes to the Doer, or bounces back to re-plan."},
    {"id": "doer", "label": "Doer", "type": "agent", "stage": "Build loop",
     "desc": "Writes the actual code + runs the tools for each subtask."},
    {"id": "refiner", "label": "Refiner", "type": "agent", "stage": "Build loop",
     "desc": "Polishes the Doer's output — cleanup, edge cases — inside the loop."},
    {"id": "feedback", "label": "Feedback", "type": "agent", "stage": "Build loop",
     "desc": "In-loop reviewer: checks each pass and feeds corrections back."},
    {"id": "loop_gate", "label": "Loop gate", "type": "gate", "stage": "Build loop",
     "desc": "Loop the Doer for another pass, or exit to validation."},
    {"id": "validator", "label": "Validator", "type": "agent", "stage": "Validate & finish",
     "desc": "Final check of the built result against the plan."},
    {"id": "validator_gate", "label": "Validator gate", "type": "gate", "stage": "Validate & finish",
     "desc": "Done, or bounce back to re-plan."},
    {"id": "learner", "label": "Learner", "type": "agent", "stage": "Validate & finish",
     "desc": "Persists durable lessons/memory so future runs start smarter."},
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
    # single multi-axis verifier (correctness+scope+risk in one call)
    {"from": "planner", "to": "verifier", "label": ""},
    {"from": "verifier", "to": "verifier_gate", "label": ""},
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
    # The 3 verify_* critics collapsed into one multi-axis `verifier` node — map
    # it (and the validator, which reuses the verifier role) to that role.
    "verifier": "verifier", "validator": "verifier",
    "doer": "doer", "refiner": "refiner",
    "feedback": "feedback", "learner": "learner",
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


# Agent nodes that actually consume the seed-injected skills/workflows/rules
# (the orchestrator + builder stages). The injection is pipeline-global, but we
# attribute it to these nodes so the graph shows WHERE the context is used —
# rather than smearing it across gates/joins that never read it.
_CONTEXT_CONSUMERS = ("enhancer", "planner", "doer")


def _base_nodes() -> list[dict]:
    out = []
    for n in _NODES:
        role = _NODE_ROLE.get(n["id"])
        out.append({**n, "tools": _tools_for(role) if role else [],
                    "status": "idle", "last_event_at": None,
                    "skills": [], "rules": [], "workflows": []})
    return out


def _overlay_ticket(nodes: list[dict], ticket: str) -> dict:
    """Mark nodes done/active from the ticket's events AND attach the skills /
    rules / workflows the run injected. Returns a run-level ``context`` summary
    (``{skills, rules, workflows}``) the UI renders as a legend."""
    context: dict[str, list] = {"skills": [], "rules": [], "workflows": []}
    try:
        from aiforge_core.tickets import store as _store
        t = _store.get(ticket)
        if not t:
            return context
        events = _store.comments(t.id, 1000)
    except Exception:
        return context
    # role/stage -> latest event time
    last: dict[str, str] = {}
    # Accumulate context_injected metadata across all such events, de-duped by
    # name (a replan re-injects the same skills — show each once).
    seen: dict[str, set] = {"skills": set(), "rules": set(), "workflows": set()}
    for e in events:
        role = e.get("agent_role") or ""
        meta = e.get("metadata") or {}
        if (e.get("kind") or "") == "context_injected":
            for bucket in ("skills", "rules", "workflows"):
                for item in (meta.get(bucket) or []):
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    if not name or name in seen[bucket]:
                        continue
                    seen[bucket].add(name)
                    context[bucket].append(item)
            continue
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
        # Attach injected context to the consuming stages so the graph shows
        # where each skill/rule/workflow was used.
        if n["id"] in _CONTEXT_CONSUMERS:
            n["skills"] = context["skills"]
            n["workflows"] = context["workflows"]
            n["rules"] = context["rules"]
    return context


def snapshot(ticket: "str | None" = None) -> dict:
    nodes = _base_nodes()
    context: dict[str, list] = {"skills": [], "rules": [], "workflows": []}
    if ticket:
        context = _overlay_ticket(nodes, ticket)
    return {"nodes": nodes, "edges": list(_EDGES), "ticket": ticket,
            "context": context}
