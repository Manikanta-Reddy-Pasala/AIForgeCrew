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
import os
import time
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
MAX_DOER_ITERS = int(os.environ.get("AIFORGE_MAX_DOER_ITERS", "3") or 3)
# Wall-clock budget for the WHOLE Doer loop (item-3 / slow 120B safety
# valve). 0 = off. When set, the loop exits with a ``partial`` verdict once
# elapsed exceeds this many seconds — so a model grinding unproductively for
# minutes ships its partial diff instead of looping until the LLM-call cap.
DOER_MAX_WALL_S = int(os.environ.get("AIFORGE_LOOP_MAX_WALL_S", "0") or 0)
# Replan cap (was GraphPipeline.max_replans=1).
MAX_REPLANS = 1
# Verifier-reject → re-plan cap (bounded inner loop).
MAX_VERIFY_REPLANS = 1
# Research-gap → re-search cap (bounded research-completeness loop).
MAX_GAP_PASSES = 1
ROUTE_RESEARCH_GAP = "research_gap"
ROUTE_RESEARCH_OK = "research_ok"

# Complexity tokens that take the trivial fast-path (skip enhancer→
# research→plan→verify). A local triage model rarely emits the exact word
# ``trivial`` — it says "simple"/"low"/"easy"/"minor"/"small" — so the gate
# treats this whole synonym set as trivial. Everything else (moderate/high/
# complex/…/unparseable) falls to the safe FULL path. Set
# AIFORGE_TRIAGE_STRICT=1 to restore exact-"trivial"-only matching.
_TRIVIAL_SYNONYMS = frozenset({
    "trivial", "simple", "low", "easy", "minor", "small",
})
# Recognised complexity words — used to decide whether a BARE model token
# (no JSON wrapper) is a genuine verdict vs prose noise. An unrecognised
# bare token falls back to "moderate" (→ FULL), so garbage never fast-paths.
_KNOWN_COMPLEXITY = _TRIVIAL_SYNONYMS | frozenset({
    "moderate", "medium", "high", "complex", "hard", "large", "difficult",
})
# Surrounding junk a sloppy model may wrap a one-word verdict in
# ("Trivial.", "**simple**", " low ", '"easy"').
_COMPLEXITY_STRIP = "`\"'*_.,:;!?()[]{}<> \t\r\n"


def _normalize_complexity(text: Any) -> str:
    """Lowercase + strip surrounding whitespace/quotes/fences/punctuation.

    Robust to a local model emitting ``"Trivial."`` / ``" simple "`` /
    ``**easy**`` — all normalise to the bare token.
    """
    return str(text).strip().strip(_COMPLEXITY_STRIP).lower()


def _coerce_complexity_token(text: Any) -> str:
    """A bare (non-JSON) model token → a recognised complexity word.

    Anything not in the known vocabulary defaults to ``"moderate"`` so a
    stray sentence never triggers the fast path (fail toward FULL)."""
    norm = _normalize_complexity(text)
    return norm if norm in _KNOWN_COMPLEXITY else "moderate"


def _triage_strict() -> bool:
    """AIFORGE_TRIAGE_STRICT=1 restores exact-"trivial"-only fast-pathing."""
    return str(os.environ.get("AIFORGE_TRIAGE_STRICT", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _is_trivial(complexity: Any) -> bool:
    """Whether a (normalised) complexity token takes the fast path."""
    token = _normalize_complexity(complexity)
    if _triage_strict():
        return token == "trivial"
    return token in _TRIVIAL_SYNONYMS


def _read_complexity(state: Any) -> str:
    """Pull the triage complexity verdict from state if present.

    Accepts ``state['complexity']`` (pre-seeded) or the triage agent's
    ``triage_verdict`` (a dict, a JSON string possibly wrapped in prose/
    fences, or a bare one-word token). Defaults to ``"moderate"`` (full
    path) when absent or unrecognised — the fast path only fires on a
    trivial-synonym signal (see :data:`_TRIVIAL_SYNONYMS`).
    """
    try:
        c = state.get("complexity")
        if isinstance(c, str) and c.strip():
            return _normalize_complexity(c) or "moderate"
        raw = state.get("triage_verdict")
        if isinstance(raw, dict):
            return _normalize_complexity(
                raw.get("complexity", "moderate")) or "moderate"
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj: Any = None
            try:
                obj = json.loads(text)
            except Exception:
                # prose {json} prose — brace-balanced fallback
                try:
                    from .rule_capture import _extract_json
                    obj = _extract_json(raw)
                except Exception:
                    obj = None
            if isinstance(obj, dict) and obj.get("complexity") is not None:
                return _normalize_complexity(obj["complexity"]) or "moderate"
            # No JSON at all → treat the raw text as a bare verdict token.
            return _coerce_complexity_token(raw)
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


def _gap_sufficient(raw: Any) -> bool:
    """True when the gap-evaluator judged research sufficient.

    Tolerant: a dict with ``sufficient`` wins; a JSON string is parsed;
    anything unparseable defaults to True so a critic formatting slip
    never traps the pipeline in a re-search loop (mirrors
    parallel_stages._coerce_verdict's fail-open stance)."""
    try:
        obj: Any = raw
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict) and "sufficient" in obj:
            return bool(obj["sufficient"])
    except Exception:
        pass
    return True


def _render_gap_brief(raw: Any) -> str:
    """Render the gap-evaluator's missing/queries into a researcher hint."""
    missing: list = []
    queries: list = []
    try:
        obj: Any = raw
        if isinstance(raw, str):
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict):
            missing = [str(m) for m in (obj.get("missing") or []) if m]
            queries = [str(q) for q in (obj.get("queries") or []) if q]
    except Exception:
        pass
    lines = ["A prior research pass was judged INCOMPLETE. Specifically "
             "locate the following before the Planner runs:"]
    for m in missing:
        lines.append(f"  - MISSING: {m}")
    for q in queries:
        lines.append(f"  - SEARCH: {q}")
    return "\n".join(lines)


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

def _force_full_pipeline() -> bool:
    return str(os.environ.get("AIFORGE_FORCE_FULL_PIPELINE", "")).strip().lower() \
        in ("1", "true", "yes", "on")


async def _triage_gate(ctx):  # type: ignore[no-untyped-def]
    complexity = _read_complexity(ctx.state)
    # Fast-path skips enhancer→research→planner→verifiers straight to the Doer
    # for a 'trivial' ticket. Set AIFORGE_FORCE_FULL_PIPELINE=1 to always take
    # the full path (every agent runs/shows) regardless of triage complexity.
    if _force_full_pipeline():
        route = ROUTE_FULL
    else:
        route = ROUTE_TRIVIAL if _is_trivial(complexity) else ROUTE_FULL
    ctx.state["graph_route"] = {"complexity": complexity, "route": route}
    ctx.route = route
    _trace(":GraphRoute", {"complexity": complexity, "route": route})


async def _loop_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    iters = int(state.get("doer_iters", 0) or 0) + 1
    state["doer_iters"] = iters
    kill = bool(state.get("loop_budget_kill"))
    # Wall-clock kill — seed the loop start on the first pass, then bail if
    # the whole loop has run past the budget. Independent of LOC churn, so it
    # protects a slow model that's looping without making (or losing) lines.
    wall_kill = False
    if DOER_MAX_WALL_S > 0:
        start = state.get("doer_loop_started_at")
        now = time.time()
        if not start:
            state["doer_loop_started_at"] = now
        elif (now - float(start)) > DOER_MAX_WALL_S:
            wall_kill = True
    if _feedback_passed(state) or kill or wall_kill or iters >= MAX_DOER_ITERS:
        if (kill or wall_kill) and not _feedback_passed(state):
            # Progress stalled / budget spent but work exists. Mark the
            # verdict ``partial`` so the runner ships the partial diff as a
            # PR (status review) instead of replaying the
            # whole pipeline. (Was the LoopAgent before-callback's job;
            # the migration moved the exit here but dropped the verdict.)
            reason = ("wall-clock budget" if wall_kill
                      else str(state.get("loop_budget_reason", "loc plateau")))
            state["feedback_verdict"] = f"partial loop_budget_kill: {reason}"
        ctx.route = ROUTE_EXIT
        _trace(":LoopExit", {"iters": iters, "kill": kill, "wall_kill": wall_kill})
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
            "doer_loop_started_at",
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


async def _gap_gate(ctx):  # type: ignore[no-untyped-def]
    """Bounded research-completeness loop. If the gap-evaluator judged
    research insufficient and we have budget, re-dispatch the context
    fan-out (route research_gap → research_entry) with a targeted hint;
    otherwise proceed to the Planner."""
    state = ctx.state
    passes = int(state.get("gap_pass_count", 0) or 0)
    if not _gap_sufficient(state.get("gap_verdict")) and passes < MAX_GAP_PASSES:
        state["gap_pass_count"] = passes + 1
        state["research_gap_brief_md"] = _render_gap_brief(
            state.get("gap_verdict"))
        ctx.route = ROUTE_RESEARCH_GAP
        _trace(":ResearchGap", {"pass": passes + 1})
    else:
        ctx.route = ROUTE_RESEARCH_OK


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
        # Re-match glob-scoped repo rules against the plan-widened scope
        # so file-scoped rules the operator seed didn't reach now load
        # (Cursor semantics: rules follow the files being touched).
        try:
            import os as _os

            from . import repo_rules
            refreshed = repo_rules.collect(
                _os.environ.get("AIFORGE_REPO_ROOT", ""), merged)
            if refreshed:
                state["rules_md"] = refreshed
        except Exception:
            pass  # rules are additive context — never block the plan


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


def make_gap_gate():
    from google.adk.workflow import node
    return node(_gap_gate, name="gap_gate")


def make_plan_promote():
    from google.adk.workflow import node
    return node(_plan_promote, name="plan_promote")


__all__ = [
    "ROUTE_TRIVIAL", "ROUTE_FULL", "ROUTE_LOOP", "ROUTE_EXIT",
    "ROUTE_REPLAN", "ROUTE_DONE", "ROUTE_VERIFY_PASS", "ROUTE_VERIFY_REPLAN",
    "ROUTE_RESEARCH_GAP", "ROUTE_RESEARCH_OK",
    "MAX_DOER_ITERS", "MAX_REPLANS", "MAX_VERIFY_REPLANS", "MAX_GAP_PASSES",
    "make_triage_gate", "make_loop_gate", "make_validator_gate",
    "make_verifier_gate", "make_plan_promote", "make_gap_gate",
    "_read_complexity", "_validator_failed", "_feedback_passed",
    "_parse_verdict", "_gap_sufficient", "_render_gap_brief", "_gap_gate",
    "_is_trivial", "_normalize_complexity", "_TRIVIAL_SYNONYMS",
]
