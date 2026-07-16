"""Route labels, env-tunable budget constants and vocab frozensets.

Split out of the former single-file ``graph_pipeline.py`` (grouped by
concern: config / scope / parsers / gates). Pure constants + the
``_int_env`` parser — no cross-group dependency. No behaviour change.
"""
from __future__ import annotations

import os
import re

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

# Recognised verdict tokens. NEGATIVE ones are scanned first so an
# ambiguous / prose-wrapped verdict that CONTAINS a reject-shaped token
# fails SAFE (→ replan / no-ship) rather than fail-open. Longer tokens
# precede their prefixes (request_changes before … , pass_with_warnings
# before pass) so the more specific verdict wins.
_VERDICT_NEGATIVE = ("request_changes", "reject", "fail")
_VERDICT_POSITIVE = ("pass_with_warnings", "approve", "pass")
