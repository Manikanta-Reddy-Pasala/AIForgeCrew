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
import re
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

def _int_env(name: str, default: int) -> int:
    """Parse an int env var, degrading to the default on garbage instead of
    crashing this module's import (which would kill the whole pipeline)."""
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


# Doer loop iteration cap (was LoopAgent.max_iterations=3). Default 4: the loop
# EXITS EARLY on a pass verdict, so extra iters cost nothing when the change is
# already green — they only give a weaker LOCAL model one more fix-attempt when
# tests/typecheck are still red. Runaway is bounded by the LOC-plateau watchdog
# (loop_budget) + DOER_MAX_WALL_S. Env-tunable: AIFORGE_MAX_DOER_ITERS.
MAX_DOER_ITERS = _int_env("AIFORGE_MAX_DOER_ITERS", 4)
# Complexity-SCALED Doer iteration ceiling. A flat cap of 4 budget-outs on a
# LARGE greenfield build (e.g. a full multi-module package + tests + README)
# before the Doer can write every file — the loop force-exits ``partial`` and
# the Validator fails. The pipeline is meant for larger tasks, so the ceiling
# scales with the triage complexity: a ``high``/``complex``/``large`` ticket
# gets many more attempts, a ``moderate`` one gets a middle budget, everything
# else keeps the base. This ONLY helps a task that is still PRODUCTIVELY adding
# lines — the LOC-plateau watchdog (loop_budget) + optional DOER_MAX_WALL_S
# still kill a STALLED loop, so a bigger ceiling never unbounds a stuck model.
# Env-tunable: AIFORGE_MAX_DOER_ITERS_MODERATE / _COMPLEX / _PER_SUBTASK / _CAP.
MAX_DOER_ITERS_MODERATE = _int_env("AIFORGE_MAX_DOER_ITERS_MODERATE", 20)
MAX_DOER_ITERS_COMPLEX = _int_env("AIFORGE_MAX_DOER_ITERS_COMPLEX", 40)
# DYNAMIC component: give the Doer this many attempts PER planned subtask, so a
# big decomposition (10 files/phases → 60 iters) gets room to finish every one
# while a 2-step plan stays lean. The ceiling is the MAX of the complexity tier
# and the plan-scaled budget — the loop still exits early on a pass verdict and
# the LOC-plateau / wall-clock watchdogs still kill a STALLED loop, so a high
# ceiling only ever helps a task that's genuinely still producing work.
ITERS_PER_SUBTASK = _int_env("AIFORGE_MAX_DOER_ITERS_PER_SUBTASK", 6)
# Hard safety ceiling so a pathological 100-subtask plan can't run unbounded.
# Runtime is really governed by the plateau/wall watchdogs; this is a backstop.
MAX_DOER_ITERS_CAP = _int_env("AIFORGE_MAX_DOER_ITERS_CAP", 200)
_COMPLEX_TOKENS = frozenset({"high", "complex", "hard", "large", "difficult"})
_MODERATE_TOKENS = frozenset({"moderate", "medium"})


_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)


def _plan_subtask_count(state: Any) -> int:
    """How many subtasks/phases the Planner decomposed the ticket into — the
    driver for the DYNAMIC iteration budget. Tries the structured extractor
    (JSON subtickets / phases) first, then falls back to counting numbered
    markdown lines directly (the extractor needs a JSON brace and returns 0 on a
    pure-markdown numbered plan). Soft-fails to 0 (→ tier floor)."""
    plan = state.get("plan_md")
    try:
        from .subtasks_callback import _extract_subtickets
        subs = _extract_subtickets(plan)
        if isinstance(subs, list) and subs:
            return len(subs)
    except Exception:  # noqa: BLE001 — never let budget sizing break the loop
        pass
    if isinstance(plan, str) and plan:
        try:
            return len(_NUMBERED_LINE_RE.findall(plan))
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _effective_max_iters(state: Any) -> int:
    """The Doer-loop iteration ceiling for THIS ticket — the MAX of the
    complexity tier and a plan-size-scaled budget (dynamic), clamped to
    :data:`MAX_DOER_ITERS_CAP`. Never below the base cap. See
    :data:`MAX_DOER_ITERS`."""
    try:
        c = _read_complexity(state)
    except Exception:  # noqa: BLE001 — a bad verdict must not unbound the loop
        c = "moderate"
    tier = MAX_DOER_ITERS
    if c in _COMPLEX_TOKENS:
        tier = MAX_DOER_ITERS_COMPLEX
    elif c in _MODERATE_TOKENS:
        tier = MAX_DOER_ITERS_MODERATE
    dynamic = _plan_subtask_count(state) * ITERS_PER_SUBTASK
    return min(MAX_DOER_ITERS_CAP, max(MAX_DOER_ITERS, tier, dynamic))
# Wall-clock budget for the WHOLE Doer loop (item-3 / slow 120B safety
# valve). 0 = off. When set, the loop exits with a ``partial`` verdict once
# elapsed exceeds this many seconds — so a model grinding unproductively for
# minutes ships its partial diff instead of looping until the LLM-call cap.
DOER_MAX_WALL_S = _int_env("AIFORGE_LOOP_MAX_WALL_S", 0)
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


# Recognised verdict tokens. NEGATIVE ones are scanned first so an
# ambiguous / prose-wrapped verdict that CONTAINS a reject-shaped token
# fails SAFE (→ replan / no-ship) rather than fail-open. Longer tokens
# precede their prefixes (request_changes before … , pass_with_warnings
# before pass) so the more specific verdict wins.
_VERDICT_NEGATIVE = ("request_changes", "reject", "fail")
_VERDICT_POSITIVE = ("pass_with_warnings", "approve", "pass")


def _parse_verdict(raw: Any) -> str | None:
    """Best-effort extract a verdict token from a dict / JSON / bare str.

    Hardened against a local model wrapping the verdict in prose
    (``I reject this because {"verdict":"reject"}``): after a clean
    ``json.loads`` fails we brace-balance-extract an embedded object
    (same helper ``parallel_stages._coerce_verdict`` uses), then scan for
    a KNOWN verdict word ANYWHERE in the text — not just the first token.
    A genuinely unparseable string returns ``None`` (the documented
    default: callers treat None as neither pass nor fail — ``_feedback_
    passed`` → False, ``_validator_failed`` → False)."""
    try:
        if isinstance(raw, dict):
            v = raw.get("verdict")
            return str(v).lower() if v is not None else None
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            # 1. clean parse (fenced or bare JSON object).
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and obj.get("verdict") is not None:
                    return str(obj["verdict"]).lower()
            except Exception:
                pass
            # 2. brace-balanced extraction — survives ``prose {json} prose``.
            try:
                from .rule_capture import _extract_json
                obj = _extract_json(raw)
                if isinstance(obj, dict) and obj.get("verdict") is not None:
                    return str(obj["verdict"]).lower()
            except Exception:
                pass
            # 3. bare-token scan anywhere. Negatives win over positives so
            #    an ambiguous verdict fails safe (→ replan), never ships.
            low = text.lower()
            for tok in _VERDICT_NEGATIVE:
                if re.search(rf"\b{tok}\b", low):
                    return tok
            for tok in _VERDICT_POSITIVE:
                if re.search(rf"\b{tok}\b", low):
                    return tok
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
    max_iters = _effective_max_iters(state)
    if _feedback_passed(state) or kill or wall_kill or iters >= max_iters:
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
        _trace(":LoopExit", {"iters": iters, "max_iters": max_iters,
                             "kill": kill, "wall_kill": wall_kill})
    else:
        # Another Doer iteration is about to run. Clear the per-iteration
        # quality signals so this next pass's Feedback gate reasons ONLY
        # over the tools that fire THIS iteration — a stale tests_ok=True
        # from a green iter-1 must not let a regressed iter-2 (that never
        # re-ran the tests) sail through. Mirrors the validator replan
        # reset (see _validator_gate). NOT cleared on the exit branch —
        # the Validator needs the final pass's values.
        _clear_state(state, ("tests_ok", "typecheck_ok", "lint_ok"))
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
