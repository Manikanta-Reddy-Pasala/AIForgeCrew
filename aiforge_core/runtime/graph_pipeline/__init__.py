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

This module was split (grouped by concern) into ``_config`` / ``_scope`` /
``_parsers`` / ``_gates`` submodules; this package re-exports the full
former top-level surface so ``from aiforge_core.runtime import
graph_pipeline`` and every ``graph_pipeline.<name>`` attribute access is
unchanged.
"""
from __future__ import annotations

from ._config import (
    DOER_MAX_WALL_S,
    ITERS_PER_SUBTASK,
    MAX_DOER_ITERS,
    MAX_DOER_ITERS_CAP,
    MAX_DOER_ITERS_COMPLEX,
    MAX_DOER_ITERS_MODERATE,
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
    _COMPLEX_TOKENS,
    _COMPLEXITY_STRIP,
    _KNOWN_COMPLEXITY,
    _MODERATE_TOKENS,
    _NUMBERED_LINE_RE,
    _TRIVIAL_SYNONYMS,
    _VERDICT_NEGATIVE,
    _VERDICT_POSITIVE,
    _int_env,
)
from ._gates import (
    log,
    make_gap_gate,
    make_loop_gate,
    make_plan_promote,
    make_triage_gate,
    make_validator_gate,
    make_verifier_gate,
    _clear_state,
    _force_full_pipeline,
    _gap_gate,
    _loop_gate,
    _plan_promote,
    _trace,
    _triage_gate,
    _validator_gate,
    _verifier_gate,
)
from ._parsers import (
    _coerce_complexity_token,
    _effective_max_iters,
    _feedback_passed,
    _gap_sufficient,
    _is_trivial,
    _normalize_complexity,
    _parse_verdict,
    _plan_subtask_count,
    _read_complexity,
    _render_gap_brief,
    _triage_strict,
    _validator_failed,
)
from ._scope import _globs_match_any_repo_file, _repo_root_for_scope

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
