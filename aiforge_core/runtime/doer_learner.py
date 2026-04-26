"""Continuous learning loop — Doer success/failure → memory.

KISS: invoked from the orchestrator AFTER a Doer run completes.
Distills run outcome into one (or two) facts: a positive when
``compile_green=1 + edit_block_ok>=1``, a negative when the run
exhausted without compile success. Persisted via Memory().retain_fact
into T3 (skills/patterns/<topic>).

No LLM call inside the distiller — KISS = template fill from
deterministic counters + last_compile_error. The Learner agent
itself can run separately for richer extraction; this module is the
always-on safety net.

Public surface:
- ``distill(ticket, doer_outcome) -> dict``
"""
from __future__ import annotations

import os
from typing import Any


def distill(ticket: object, doer_outcome: dict[str, Any]) -> dict | None:
    """Persist a T3 fact summarising the Doer outcome. No-op when
    ``AIFORGE_DOER_AUTOLEARN=0``. Best-effort: never raises."""
    if os.environ.get("AIFORGE_DOER_AUTOLEARN", "1") != "1":
        return None

    identifier = getattr(ticket, "identifier", "?")
    title = (getattr(ticket, "title", "") or "")[:120]
    edit_ok = int(doer_outcome.get("edit_block_ok") or 0)
    compile_green = int(doer_outcome.get("compile_green") or 0)
    stop_reason = doer_outcome.get("stop_reason") or "unknown"
    summary = (doer_outcome.get("summary") or "")[:600]
    files = doer_outcome.get("files_touched") or []

    if compile_green and edit_ok:
        worked = True
        text = (
            f"Doer pattern · ticket {identifier} · COMPILE-GREEN\n"
            f"Title: {title}\n"
            f"Edits: {edit_ok}, files: {', '.join(files[:6])}\n"
            f"Approach: {summary}"
        )
    else:
        worked = False
        last_err = (doer_outcome.get("last_compile_error") or "")[:600]
        text = (
            f"Doer attempt · ticket {identifier} · {stop_reason.upper()}\n"
            f"Title: {title}\n"
            f"Edits attempted: {edit_ok}, compile_green: {compile_green}\n"
            f"Failure: {summary}\n"
            f"Compile error tail: {last_err}"
        )

    try:
        from .memory import Memory
        mem = Memory()
        rid = mem.retain_fact(
            text=text, tier="t3",
            wing=f"patterns/doer-{'success' if worked else 'failure'}",
            kind="doer_outcome",
            source=f"doer-autolearn:{identifier}",
            metadata={
                "ticket": identifier,
                "worked": worked,
                "edit_block_ok": edit_ok,
                "compile_green": compile_green,
                "files": files[:10],
                "stop_reason": stop_reason,
            },
        )
        return {"id": rid, "worked": worked, "tier": "t3"}
    except Exception as exc:
        print(f"[doer_learner] retain failed: {exc}")
        return None
