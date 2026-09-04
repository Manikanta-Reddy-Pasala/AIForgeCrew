"""Router node bodies + ``FunctionNode`` factories for the v6 graph.

Split out of the former single-file ``graph_pipeline.py`` (grouped by
concern). Wires the deterministic gates on top of :mod:`._config`,
:mod:`._parsers` and :mod:`._scope`. No behaviour change.
"""
from __future__ import annotations

import json
import logging
import os
import time

from ._config import (
    DOER_MAX_WALL_S,
    MAX_GAP_PASSES,
    MAX_REPLANS,
    MAX_VERIFY_REPLANS,
    ROUTE_DONE,
    ROUTE_EXIT,
    ROUTE_FULL,
    ROUTE_LOOP,
    ROUTE_REPLAN,
    ROUTE_RESEARCH_GAP,
    ROUTE_RESEARCH_OK,
    ROUTE_TRIVIAL,
    ROUTE_VERIFY_PASS,
    ROUTE_VERIFY_REPLAN,
    _VERDICT_NEGATIVE,
)
from ._parsers import (
    _effective_max_iters,
    _feedback_passed,
    _gap_sufficient,
    _is_trivial,
    _parse_verdict,
    _read_complexity,
    _render_gap_brief,
    _validator_failed,
)
from ._scope import _globs_match_any_repo_file, _repo_root_for_scope

log = logging.getLogger("aiforge.graph_pipeline")


# ── router node bodies ──────────────────────────────────────────────────
# Each takes the workflow Context (param named ``ctx`` is bound to the
# Context, not to state) and sets ``ctx.route``.

def _force_full_pipeline() -> bool:
    return str(os.environ.get("AIFORGE_FORCE_FULL_PIPELINE", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _triage_gate(ctx):  # type: ignore[no-untyped-def]
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


def _wall_clock_kill(state) -> bool:
    """Seed the loop start on the first pass, then bail if the whole loop has
    run past the budget. Independent of LOC churn, so it protects a slow model
    that's looping without making (or losing) lines."""
    if DOER_MAX_WALL_S <= 0:
        return False
    start = state.get("doer_loop_started_at")
    now = time.time()
    if not start:
        state["doer_loop_started_at"] = now
        return False
    return (now - float(start)) > DOER_MAX_WALL_S


def _exhaustion_reason(wall_kill: bool, cap_out: bool, max_iters: int,
                       state) -> str:
    if wall_kill:
        return "wall-clock budget"
    if cap_out:
        return f"iteration ceiling ({max_iters})"
    return str(state.get("loop_budget_reason", "loc plateau"))


# Per-iteration quality signals, cleared before the NEXT Doer pass so its
# Feedback gate reasons ONLY over the tools that fire THAT iteration.
_PER_ITER_KEYS = (
    "tests_ok", "typecheck_ok", "lint_ok",
    # `doer_incomplete` is written when a Doer turn stops early or lands zero
    # edits, and the Feedback quality gate turns it into a hard fail. Nothing
    # ever cleared it, so ONE bad turn made the pass-exit unreachable for the
    # rest of the run AND for the replanned attempt: the loop then ground to
    # its ceiling with a green tree and a model saying "pass" every iteration.
    "doer_incomplete",
    # The repeat guard counts identical (tool, args) calls for the whole RUN.
    # `run_tests` with byte-identical args is the normal case once per
    # iteration, so from iteration 4 the guard short-circuited it — and ADK
    # still fires the after-tool callback, which recorded the green suite as
    # tests_ok=False.
    "_repeat_counts",
)


def _loop_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    iters = int(state.get("doer_iters", 0) or 0) + 1
    state["doer_iters"] = iters
    kill = bool(state.get("loop_budget_kill"))
    wall_kill = _wall_clock_kill(state)
    max_iters = _effective_max_iters(state)
    cap_out = iters >= max_iters
    passed = _feedback_passed(state)
    if not (passed or kill or wall_kill or cap_out):
        # Another Doer iteration is about to run. NOT cleared on the exit
        # branch — the Validator needs the final pass's values. Mirrors the
        # validator replan reset (see _validator_gate).
        _clear_state(state, _PER_ITER_KEYS)
        ctx.route = ROUTE_LOOP
        _trace(":LoopContinue", {"iters": iters, "max_iters": max_iters})
        return
    if not passed:
        # Progress stalled / budget spent but work exists. Mark the verdict
        # ``partial`` so the runner ships the partial diff as a PR (status
        # review) instead of replaying the whole pipeline. (Was the LoopAgent
        # before-callback's job; the migration moved the exit here but dropped
        # the verdict.)
        # The ceiling is spent work too. Leaving THIS branch's verdict as a
        # plain `fail` meant the validator's "don't replan a stalled loop"
        # guard did not fire, so the whole pipeline re-ran and hit the same
        # ceiling again — double the iterations for the same result, on the
        # exhaustion path that is by far the most common when iterations are
        # fast.
        state["feedback_verdict"] = ("partial loop_budget_kill: "
                                     + _exhaustion_reason(wall_kill, cap_out,
                                                          max_iters, state))
    ctx.route = ROUTE_EXIT
    _trace(":LoopExit", {"iters": iters, "max_iters": max_iters,
                         "kill": kill, "wall_kill": wall_kill})


def _validator_gate(ctx):  # type: ignore[no-untyped-def]
    state = ctx.state
    replans = int(state.get("replan_count", 0) or 0)
    # Loop-engineering STOP condition (plateau/replan cap). If the Doer
    # loop exited on a loop_budget_kill (LOC-plateau or wall-clock budget),
    # the work is already as far as this local model will carry it. A
    # replan just re-runs the SAME model on the SAME attempted work and it
    # re-plateaus — a full wasted planner→verify→doer cycle (ONE-157 burnt
    # ~24 min this way on an already-committed diff). Ship what exists to
    # review instead of re-planning finished/stalled work; the runner maps
    # partial+PR → in_review and partial+no-PR → blocked. Verifier replans
    # (pre-Doer plan rejection) are unaffected — those DO help.
    fv = str(state.get("feedback_verdict") or "")
    if "loop_budget_kill" in fv:
        state["_no_replan_reason"] = "doer_plateau"
        ctx.route = ROUTE_DONE
        _trace(":ValidatorNoReplanPlateau", {"feedback_verdict": fv[:80]})
        return
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
            # Both are run-scoped and both make a pass unreachable; a
            # replanned attempt that inherits them is spent before it starts.
            "doer_incomplete", "_repeat_counts",
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


def _verifier_gate(ctx):  # type: ignore[no-untyped-def]
    """Act on the merged verifier verdict. A rejected plan loops back to
    the Planner ONCE (bounded) instead of handing a known-bad plan to the
    Doer; otherwise proceed to the Doer."""
    state = ctx.state
    verdict = _parse_verdict(state.get("verifier_verdict"))
    vreplans = int(state.get("verify_replan_count", 0) or 0)
    # Any NEGATIVE verdict replans — not just the literal "reject". A small local
    # model that phrases rejection as "fail"/"request_changes" (or wraps it in
    # prose) must not slip a known-bad plan through to the Doer. Mirrors
    # _validator_failed. An unparseable verdict → None → proceed (documented).
    if verdict in _VERDICT_NEGATIVE and vreplans < MAX_VERIFY_REPLANS:
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


def _gap_gate(ctx):  # type: ignore[no-untyped-def]
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


def _plan_object(raw) -> dict | None:
    """The Planner's JSON blob, from a bare object, a fenced one, or one
    embedded in markdown prose. None when nothing parses."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except Exception:  # noqa: BLE001
            return None
    return obj if isinstance(obj, dict) else None


def _tests_declared(obj: dict) -> bool:
    """Does the plan DECLARE a test bar?

    ``tests_declared`` is what AIFORGE_STRICT_TEST_GATE keys on — and nothing
    in the codebase ever wrote it, so that gate was inert: an operator could
    switch it on and get no strictness at all. The plan is the one place the
    bar is stated.
    """
    declared = obj.get("tests_declared")
    if declared is not None:
        return bool(declared)
    acceptance = json.dumps(obj.get("acceptance")
                            or obj.get("acceptance_criteria") or "")
    return ("test" in acceptance.lower()
            or any("test" in str(st.get("acceptance", "")).lower()
                   for st in (obj.get("subtickets") or [])
                   if isinstance(st, dict)))


def _plan_globs(obj: dict) -> list[str]:
    """Every scope glob the plan declares, top-level and per-subticket."""
    globs: list[str] = []
    top = obj.get("scope_allowlist_globs")
    if isinstance(top, list):
        globs += [str(g) for g in top if g]
    for st in obj.get("subtickets") or []:
        if isinstance(st, dict) and isinstance(
                st.get("scope_allowlist_globs"), list):
            globs += [str(g) for g in st["scope_allowlist_globs"] if g]
    return globs


def _merge_seeded(state, globs: list[str]) -> list[str]:
    """Operator-seeded globs live in a SEPARATE durable key — the runner writes
    both keys at init. Replans clear scope_allowlist_globs (so the rejected
    plan's globs don't widen scope forever) but never the seeded key; the live
    key is the back-compat fallback."""
    seeded = state.get("scope_allowlist_globs_seeded")
    if not isinstance(seeded, list):
        seeded = state.get("scope_allowlist_globs")
    if isinstance(seeded, list):
        globs = list(seeded) + [g for g in globs if g not in seeded]
    seen: set = set()
    return [g for g in globs if not (g in seen or seen.add(g))]


def _usable_scope(merged: list[str]) -> list[str]:
    """FAIL-OPEN on a bad plan: if the (non-empty) globs match ZERO files in the
    actual repo they are wrong for THIS repo's layout (a common failure when the
    architect assumes a src/... layout, or another repo's paths). An allowlist
    that matches nothing blocks EVERY Doer edit → no changes → loc-plateau kill
    + a spurious scope_violation. Treat it as no-scope (allow the whole
    worktree — which is already the per-ticket sandbox)."""
    if merged and not _globs_match_any_repo_file(merged):
        log.warning("scope globs match NO file in the repo — clearing "
                    "(fail-open to repo scope): %s", merged)
        return []
    return merged


def _refresh_scoped_rules(state, merged: list[str]) -> None:
    """Re-match glob-scoped repo rules against the plan-widened scope so
    file-scoped rules the operator seed didn't reach now load (Cursor
    semantics: rules follow the files being touched). Rules are additive
    context — a failure never blocks the plan."""
    try:
        from .. import repo_rules
        refreshed = repo_rules.collect(_repo_root_for_scope(), merged)
        if refreshed:
            state["rules_md"] = refreshed
    except Exception:  # noqa: BLE001
        pass


def _plan_promote(ctx):  # type: ignore[no-untyped-def]
    """Promote structured fields out of the Planner's raw ``plan_md``.

    The Planner emits one JSON blob under ``plan_md``; nothing else
    parsed it, so ``scope_allowlist_globs`` from the plan's subtickets
    never reached the state key scope_guard / verify_scope judge.
    Union the plan's globs with any operator-seeded ones. Soft-fail —
    an unparseable plan leaves state untouched.
    """
    state = ctx.state
    obj = _plan_object(state.get("plan_md"))
    if obj is None:
        return
    try:
        state["tests_declared"] = _tests_declared(obj)
    except Exception:  # noqa: BLE001 — never let this break plan promotion
        pass
    merged = _usable_scope(_merge_seeded(state, _plan_globs(obj)))
    if merged:
        state["scope_allowlist_globs"] = merged
        _refresh_scoped_rules(state, merged)


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
        from ..tools._trace import emit
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
