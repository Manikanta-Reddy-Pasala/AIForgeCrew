"""Auto-mine each finished run for durable memory (frontier gaps #1,2,5).

Wires AiForgeMemory's ``consolidate()`` in as an after-callback on the
terminal pipeline stage: build a trajectory text from session state →
extract durable facts → decide ADD/UPDATE/DELETE/NOOP vs existing similar
memory → write via the bi-temporal store, plus a reflection.

Complements ``learner_persist`` (which only persists the Learner's
EXPLICIT ``facts_json``) by capturing the durable knowledge the Learner
didn't bother to emit.

This was backed by the optional AiForgeMemory graph consolidate primitives
(supersede/invalidate), which have been removed (SQLite-only build), so it
is a soft no-op. Never blocks the pipeline.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.memory_consolidate")


def _is_disabled() -> bool:
    return os.environ.get("AIFORGE_MEMORY_CONSOLIDATE_DISABLE", "0") in {
        "1", "true", "True"}


def _passed(state) -> bool:
    """Only consolidate a SUCCESSFUL run — a failed run's lessons go to
    failure_memory, not the durable fact store."""
    v = state.get("feedback_verdict") or state.get("validator_verdict") or ""
    s = str(v).lower()
    return any(tok in s for tok in ("pass", "approve"))


def _trajectory_text(state) -> str:
    parts: list[str] = []
    for key, label in (
        ("enhanced_body", "TICKET"),
        ("plan_md", "PLAN"),
        ("doer_outcome", "DOER OUTCOME (diffs + test/compile status)"),
        ("validator_verdict", "VALIDATOR VERDICT"),
        ("feedback_verdict", "FEEDBACK VERDICT"),
    ):
        v = state.get(key)
        if v:
            parts.append(f"## {label}\n{str(v)[:4000]}")
    return "\n\n".join(parts)


def _skip_reason(state, repo: str, traj: str) -> str | None:
    """Why this run should NOT be mined, or None to proceed."""
    if _is_disabled():
        return "disabled"
    if not _passed(state):
        return "not a passing run"
    if not repo:
        return "no repo"
    if not traj:
        return "no trajectory"
    return None


def run_consolidation(state) -> dict:
    """Synchronous body. The graph-backed consolidation store was removed
    (SQLite-only build), so this is a soft no-op. Never raises."""
    try:
        repo = (state.get("ticket_project")
                or os.environ.get("AIFORGE_AFM_REPO", "") or "")
        traj = _trajectory_text(state) if repo else ""
        skip = _skip_reason(state, repo, traj)
        if skip:
            return {"skipped": skip}
        return {"skipped": "consolidation backend removed (SQLite-only build)"}
    except Exception as exc:  # noqa: BLE001 — must never break the pipeline
        log.warning("memory_consolidate failed: %s", exc)
        return {"error": str(exc)}


def make_consolidate_after_callback():
    """ADK ``after_agent_callback`` that mines the finished run for memory."""
    async def _callback(*, callback_context, **_kw):
        try:
            run_consolidation(callback_context.state)
        except Exception as exc:  # noqa: BLE001
            log.warning("memory_consolidate callback failed: %s", exc)
        return None
    return _callback


__all__ = ["run_consolidation", "make_consolidate_after_callback"]
