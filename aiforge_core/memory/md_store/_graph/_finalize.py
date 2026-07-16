"""Graph layer — the cross-brief RULES orchestrator run after every fold. Wires
the deterministic + LLM graph rules (merge / dedupe / sweep / lint / contradict
/ map) into one best-effort, idempotent pass. The top layer; builds on
``_map``, ``_lint`` and ``_reconcile``."""
from __future__ import annotations

import os

from .._base import iter_briefs
from .._compact import sweep_empty_briefs
from ._lint import lint_graph
from ._map import map_scopes
from ._reconcile import dedupe_global_copies, merge_similar_topics, resolve_contradictions


def _briefs_modified_within(hours: float) -> list[str]:
    """Keys of scope briefs whose file changed within ``hours`` — the set the
    just-finished fold touched. Empty when nothing changed this cycle."""
    import time
    cutoff = time.time() - hours * 3600
    out: list[str] = []
    for p in iter_briefs():
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p.stem[len("compacted-"):])
        except OSError:
            continue
    return out


def finalize_briefs(*, role: str = "learner", recent_only: bool = False) -> dict:
    """Run the cross-brief RULES after a fold so EVERY compaction — the plain
    hourly/`Compact` path, not just `Compact all` — applies them without miss:
    merge near-duplicate topics → drop global-duplicate facts → resolve
    cross-scope contradictions (latest value wins) → sweep briefs those steps
    emptied → lint dangling links → (re)link related briefs bidirectionally.

    ``recent_only`` (the HOURLY path): the cheap deterministic rules always run,
    but the LLM steps (contradict, map_scopes) are SKIPPED when no brief changed
    in the last ``AIFORGE_COMPACT_RECENT_H`` hours (default 1) — so an idle hour
    does no model work. The nightly ``Compact all`` runs unconditionally (full).
    Each step is best-effort + idempotent; a failure never blocks the rest."""
    llm = True
    if recent_only:
        try:
            win = float(os.environ.get("AIFORGE_COMPACT_RECENT_H", "1"))
        except (TypeError, ValueError):
            win = 1.0
        # a small grace over the interval so a brief written just before the tick
        # still counts; skip the LLM rules only when truly nothing changed.
        llm = bool(_briefs_modified_within(win * 1.5))
    steps = [
        ("merge_topics", lambda: merge_similar_topics()),
        ("dedupe_global", lambda: dedupe_global_copies()),
        ("sweep_empty", lambda: sweep_empty_briefs(archive=True)),
        ("lint_graph", lambda: lint_graph(repair=True)),
    ]
    if llm:
        steps[2:2] = [("contradict", lambda: resolve_contradictions(role=role))]
        steps.append(("map_scopes", lambda: map_scopes(role=role)))
    else:
        steps.append(("llm_rules", lambda: {"skipped": "no recent brief changes"}))
    out: dict = {}
    for name, fn in steps:
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 — one rule failing must not block others
            out[name] = {"error": str(exc)}
    return out
