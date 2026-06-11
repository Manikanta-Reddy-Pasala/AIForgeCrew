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
ROUTE_VERIFY_PASS = "verify_pass"
ROUTE_VERIFY_REPLAN = "verify_replan"

# Doer loop iteration cap (was LoopAgent.max_iterations=3).
MAX_DOER_ITERS = 3
# Replan cap (was GraphPipeline.max_replans=1).
MAX_REPLANS = 1
# Verifier-reject → re-plan cap (bounded inner loop).
MAX_VERIFY_REPLANS = 1


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
    iters = int(state.get("doer_iters", 0) or 0) + 1
    state["doer_iters"] = iters
    kill = bool(state.get("loop_budget_kill"))
    if _feedback_passed(state) or kill or iters >= MAX_DOER_ITERS:
        if kill and not _feedback_passed(state):
            # LOC-plateau kill: progress stalled but work exists. Mark the
            # verdict ``partial`` so the runner ships the partial diff as a
            # PR (status review) instead of claude_takeover replaying the
            # whole pipeline. (Was the LoopAgent before-callback's job;
            # the migration moved the exit here but dropped the verdict.)
            reason = str(state.get("loop_budget_reason", "loc plateau"))
            state["feedback_verdict"] = f"partial loop_budget_kill: {reason}"
        ctx.route = ROUTE_EXIT
        _trace(":LoopExit", {"iters": iters, "kill": kill})
    else:
        ctx.route = ROUTE_LOOP


async def _validator_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    replans = int(state.get("replan_count", 0) or 0)
    if _validator_failed(state) and replans < MAX_REPLANS:
        state["replan_count"] = replans + 1
        # Reset ALL loop-scoped state so the re-planned attempt starts
        # clean. Resetting only doer_iters left a stale feedback_verdict
        # / loop_budget_kill from the prior pass — loop_gate would read
        # the old "pass" (or kill flag) and EXIT the Doer loop at zero
        # real iterations, silently wasting the replan.
        state["doer_iters"] = 0
        _clear_state(state, (
            "feedback_verdict", "loop_budget_kill", "loop_budget_reason",
            "loc_history", "loc_first_seen", "doer_outcome",
            "verifier_verdict", "verify_correctness", "verify_scope",
            "verify_risk", "verify_replan_count",
            # quality-gate signals: a stale tests_ok=False from the failed
            # pass would force Feedback's gate to fail the replanned pass
            # unless the new Doer happens to re-run run_tests.
            "tests_ok", "typecheck_ok", "lint_ok",
            # plan-derived scope: cleared so plan_promote re-derives from
            # the NEW plan (+ operator seeds) instead of monotonically
            # widening with the rejected plan's globs.
            "scope_allowlist_globs"))
        state["replan_note"] = (
            f"Validator requested changes (replan {replans + 1}). The prior "
            "plan did not land cleanly — re-plan SMALLER: split the failing "
            "subticket, tighten scope, add the missing test."
        )
        ctx.route = ROUTE_REPLAN
        _trace(":Replan", {"replan": replans + 1})
    else:
        ctx.route = ROUTE_DONE


async def _verifier_gate(ctx):  # type: ignore[no-untyped-def]
    """Act on the merged verifier verdict. A rejected plan loops back to
    the Planner ONCE (bounded) instead of handing a known-bad plan to the
    Doer; otherwise proceed to the Doer."""
    state = ctx.state
    verdict = _parse_verdict(state.get("verifier_verdict"))
    vreplans = int(state.get("verify_replan_count", 0) or 0)
    if verdict == "reject" and vreplans < MAX_VERIFY_REPLANS:
        state["verify_replan_count"] = vreplans + 1
        vv = state.get("verifier_verdict") or {}
        why = vv.get("rationale", "verifier rejected the plan") \
            if isinstance(vv, dict) else "verifier rejected the plan"
        state["replan_note"] = (
            f"Verifier rejected the plan ({why}). Re-plan addressing the "
            "rejection before any code is written."
        )
        # clear stale per-axis verdicts + plan-derived scope so the
        # re-plan's verifier pass and plan_promote run fresh
        _clear_state(state, ("verifier_verdict", "verify_correctness",
                             "verify_scope", "verify_risk",
                             "scope_allowlist_globs"))
        ctx.route = ROUTE_VERIFY_REPLAN
        _trace(":VerifyReplan", {"replan": vreplans + 1, "why": why})
    else:
        ctx.route = ROUTE_VERIFY_PASS


async def _plan_promote(ctx):  # type: ignore[no-untyped-def]
    """Promote structured fields out of the Planner's raw ``plan_md``.

    The Planner emits one JSON blob under ``plan_md``; nothing else
    parsed it, so ``scope_allowlist_globs`` from the plan's subtickets
    never reached the state key scope_guard / verify_scope judge.
    Union the plan's globs with any operator-seeded ones. Soft-fail —
    an unparseable plan leaves state untouched.
    """
    state = ctx.state
    raw = state.get("plan_md")
    if not isinstance(raw, str) or not raw.strip():
        return
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    try:
        obj = json.loads(text)
    except Exception:
        # plan may be markdown with an embedded JSON object — best-effort
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return
        try:
            obj = json.loads(text[start:end + 1])
        except Exception:
            return
    if not isinstance(obj, dict):
        return
    globs: list[str] = []
    top = obj.get("scope_allowlist_globs")
    if isinstance(top, list):
        globs += [str(g) for g in top if g]
    for st in obj.get("subtickets") or []:
        if isinstance(st, dict):
            sg = st.get("scope_allowlist_globs")
            if isinstance(sg, list):
                globs += [str(g) for g in sg if g]
    # Operator-seeded globs live in a SEPARATE durable key — the runner
    # writes both keys at init. Replans clear scope_allowlist_globs (so
    # the rejected plan's globs don't widen scope forever) but never the
    # seeded key. Fall back to the live key for back-compat.
    seeded = state.get("scope_allowlist_globs_seeded")
    if not isinstance(seeded, list):
        seeded = state.get("scope_allowlist_globs")
    if isinstance(seeded, list):
        globs = list(seeded) + [g for g in globs if g not in seeded]
    # dedupe, keep order
    seen: set = set()
    merged = [g for g in globs if not (g in seen or seen.add(g))]
    if merged:
        state["scope_allowlist_globs"] = merged


def _clear_state(state, keys) -> None:
    """Drop keys from session state. ADK's ``State`` has no ``pop`` (only
    item set/get), so fall back to setting ``None`` — the parse helpers
    and ``{key?}`` templating both treat None as absent."""
    for k in keys:
        try:
            state.pop(k, None)  # plain dict (tests)
        except AttributeError:
            try:
                state[k] = None  # ADK State delta
            except Exception:
                pass


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


def make_verifier_gate():
    from google.adk.workflow import node
    return node(_verifier_gate, name="verifier_gate")


def make_plan_promote():
    from google.adk.workflow import node
    return node(_plan_promote, name="plan_promote")


__all__ = [
    "ROUTE_TRIVIAL", "ROUTE_FULL", "ROUTE_LOOP", "ROUTE_EXIT",
    "ROUTE_REPLAN", "ROUTE_DONE", "ROUTE_VERIFY_PASS", "ROUTE_VERIFY_REPLAN",
    "MAX_DOER_ITERS", "MAX_REPLANS", "MAX_VERIFY_REPLANS",
    "make_triage_gate", "make_loop_gate", "make_validator_gate",
    "make_verifier_gate", "make_plan_promote",
    "_read_complexity", "_validator_failed", "_feedback_passed",
    "_parse_verdict",
]
