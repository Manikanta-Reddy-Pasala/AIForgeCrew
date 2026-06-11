"""Routing FunctionNodes for the native ADK ``Workflow`` graph.

ADK 2.x deprecated ``ParallelAgent`` / ``LoopAgent`` in favour of the
graph-based :class:`google.adk.workflow.Workflow`: explicit nodes wired
by ``Edge``s, with conditional routing expressed by an edge ``route``
value a node emits (``ctx.route = "..."``). This module holds the three
deterministic routers that drive the v6 graph; the agents themselves are
plain ``LlmAgent`` graph nodes (see :mod:`pipeline`).

* **triage_gate** — fast-path switch. ``trivial`` ticket routes straight
  to the Doer; everything else takes the full enhance→context→plan path.
* **loop_gate** — replaces ``LoopAgent``'s internal counter. Reads the
  Feedback verdict + an iteration counter (+ the LOC-plateau kill flag)
  and routes ``loop`` back to the Doer or ``exit`` to the Validator.
* **validator_gate** — replan edge. A failed Validator routes ``replan``
  back to the Planner once (resetting the loop counter); otherwise
  ``done`` to the Learner.

Each gate is wrapped as a ``FunctionNode`` via :func:`make_*` so the
pipeline can name them and wire edges. Routes are plain strings matched
against ``Edge.route``.
"""
from __future__ import annotations

import json
from typing import Any

# Route label constants — keep in sync with the edge wiring in pipeline.py.
ROUTE_TRIVIAL = "trivial"
ROUTE_FULL = "full"
ROUTE_LOOP = "loop"
ROUTE_EXIT = "exit"
ROUTE_REPLAN = "replan"
ROUTE_DONE = "done"

# Doer loop iteration cap (was LoopAgent.max_iterations=3).
MAX_DOER_ITERS = 3
# Replan cap (was GraphPipeline.max_replans=1).
MAX_REPLANS = 1


def _read_complexity(state: Any) -> str:
    """Pull the triage complexity verdict from state if present.

    Accepts ``state['complexity']`` (pre-seeded) or the triage agent's
    ``triage_verdict`` JSON. Defaults to ``"moderate"`` (full path) when
    absent — the fast path only fires on an explicit ``trivial`` signal.
    """
    try:
        c = state.get("complexity")
        if isinstance(c, str) and c:
            return c.lower()
        raw = state.get("triage_verdict")
        if isinstance(raw, dict):
            return str(raw.get("complexity", "moderate")).lower()
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
            if isinstance(obj, dict):
                return str(obj.get("complexity", "moderate")).lower()
    except Exception:
        pass
    return "moderate"


def _parse_verdict(raw: Any) -> str | None:
    """Best-effort extract a verdict token from a dict / JSON / bare str."""
    try:
        if isinstance(raw, dict):
            v = raw.get("verdict")
            return str(v).lower() if v is not None else None
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and obj.get("verdict") is not None:
                    return str(obj["verdict"]).lower()
            except Exception:
                pass
            return text.split()[0].lower() if text.split() else None
    except Exception:
        pass
    return None


def _validator_failed(state: Any) -> bool:
    """True when the Validator asked for changes (the replan trigger)."""
    v = _parse_verdict(state.get("validator_verdict"))
    return v in ("request_changes", "reject", "fail") if v else False


def _feedback_passed(state: Any) -> bool:
    v = _parse_verdict(state.get("feedback_verdict"))
    return v in ("pass", "approve", "pass_with_warnings") if v else False


# ── router node bodies ──────────────────────────────────────────────────
# Each takes the workflow Context (param named ``ctx`` is bound to the
# Context, not to state) and sets ``ctx.route``.

async def _triage_gate(ctx):  # type: ignore[no-untyped-def]
    complexity = _read_complexity(ctx.state)
    route = ROUTE_TRIVIAL if complexity == "trivial" else ROUTE_FULL
    ctx.state["graph_route"] = {"complexity": complexity, "route": route}
    ctx.route = route
    _trace(":GraphRoute", {"complexity": complexity, "route": route})


async def _loop_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    iters = int(state.get("doer_iters", 0)) + 1
    state["doer_iters"] = iters
    kill = bool(state.get("loop_budget_kill"))
    if _feedback_passed(state) or kill or iters >= MAX_DOER_ITERS:
        ctx.route = ROUTE_EXIT
        _trace(":LoopExit", {"iters": iters, "kill": kill})
    else:
        ctx.route = ROUTE_LOOP


async def _validator_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    replans = int(state.get("replan_count", 0))
    if _validator_failed(state) and replans < MAX_REPLANS:
        state["replan_count"] = replans + 1
        # Reset the Doer loop counter so the re-planned attempt gets a
        # fresh budget.
        state["doer_iters"] = 0
        state["replan_note"] = (
            f"Validator requested changes (replan {replans + 1}). The prior "
            "plan did not land cleanly — re-plan SMALLER: split the failing "
            "subticket, tighten scope, add the missing test."
        )
        ctx.route = ROUTE_REPLAN
        _trace(":Replan", {"replan": replans + 1})
    else:
        ctx.route = ROUTE_DONE


def _trace(label: str, payload: dict) -> None:
    try:
        from .tools._trace import emit
        emit(label, payload)
    except Exception:
        pass


# ── FunctionNode factories ──────────────────────────────────────────────

def make_triage_gate():
    from google.adk.workflow import node
    return node(_triage_gate, name="triage_gate")


def make_loop_gate():
    from google.adk.workflow import node
    return node(_loop_gate, name="loop_gate")


def make_validator_gate():
    from google.adk.workflow import node
    return node(_validator_gate, name="validator_gate")


__all__ = [
    "ROUTE_TRIVIAL", "ROUTE_FULL", "ROUTE_LOOP", "ROUTE_EXIT",
    "ROUTE_REPLAN", "ROUTE_DONE", "MAX_DOER_ITERS", "MAX_REPLANS",
    "make_triage_gate", "make_loop_gate", "make_validator_gate",
    "_read_complexity", "_validator_failed", "_feedback_passed",
    "_parse_verdict",
]
