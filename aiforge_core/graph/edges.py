from __future__ import annotations

from langgraph.graph import END

from .state import AgentState


def route_from_supervisor(state: AgentState) -> str:
    stop = state.get("stop_reason")
    if stop in ("done", "blocked"):
        return END
    role = (state.get("ticket") or {}).get("assignee_role") or ""
    mapping = {
        "planner": "planner_node",
        "sr_developer": "planner_node",
        "doer": "doer_node",
        "developer": "doer_node",
        "learner": "learner_node",
        "fact_extract": "learner_node",
    }
    return mapping.get(role, "planner_node")


def after_doer(state: AgentState) -> str:
    stop = state.get("stop_reason")
    if stop in ("done", "blocked"):
        return END
    if (state.get("compile_fail_count") or 0) >= 2:
        return "planner_node"
    return "feedback_node"


def after_feedback(state: AgentState) -> str:
    stop = state.get("stop_reason")
    if stop in ("done", "blocked"):
        return END
    verdict = state.get("verdict")
    if verdict == "pass":
        return "learner_node"
    if verdict == "scope_violation":
        return END
    return "doer_node"
