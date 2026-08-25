"""Auto-mine each finished run for durable memory (frontier gaps #1,2,5).

Wires AiForgeMemory's ``consolidate()`` in as an after-callback on the
terminal pipeline stage: build a trajectory text from session state →
extract durable facts → decide ADD/UPDATE/DELETE/NOOP vs existing similar
memory → write via the bi-temporal store, plus a reflection.

Complements ``learner_persist`` (which only persists the Learner's
EXPLICIT ``facts_json``) by capturing the durable knowledge the Learner
didn't bother to emit, and by reconciling against what's already stored
instead of blindly appending. Feature-flagged, soft-fail, neo4j-only
(skips the embedded SQLite fallback — consolidation needs the graph
store's supersede/invalidate primitives). Never blocks the pipeline.
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


def _llm_fn():
    from aiforge_core.llm import client

    def _f(system: str, user: str) -> str:
        return client.complete(
            "learner",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            max_tokens=1200, timeout_s=120,
        )
    return _f


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


def _embed_fn():
    """The embedder, or a no-op when it is unavailable."""
    try:
        from aiforge_core.memory.embed import embed as embed_fn  # type: ignore
        return embed_fn
    except Exception:  # noqa: BLE001
        return lambda _t: None


def _event_time_for(tid) -> float | None:
    """Bi-temporal: stamp event_time from the ticket's created_at when we can,
    so the mined facts are valid_at the real work time."""
    if not tid:
        return None
    try:
        from aiforge_core.tickets.store import get as ticket_get
        t = ticket_get(tid)
        created = getattr(t, "created_at", None) if t else None
        return created.timestamp() if created is not None else None
    except Exception:  # noqa: BLE001
        return None


def run_consolidation(state) -> dict:
    """Synchronous body — extract/decide/apply/reflect over the run.
    Returns a small status dict; never raises."""
    try:
        repo = (state.get("ticket_project")
                or os.environ.get("AIFORGE_AFM_REPO", "") or "")
        traj = _trajectory_text(state) if repo else ""
        skip = _skip_reason(state, repo, traj)
        if skip:
            return {"skipped": skip}
        from .learner_persist import _open_driver
        driver = _open_driver()
        if driver is None:
            return {"skipped": "no neo4j driver (embedded mode)"}
        tid = state.get("ticket_identifier")
        try:
            from aiforge_memory.features.memory import consolidate as _C
            return _C.consolidate(
                driver, repo=repo, trajectory_text=traj, llm_fn=_llm_fn(),
                embed_fn=_embed_fn(), author="consolidator",
                tags=([f"ticket:{tid}"] if tid else []),
                event_time=_event_time_for(tid))
        finally:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
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
