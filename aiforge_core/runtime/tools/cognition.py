"""Lightweight cognition tools: ``think`` (no-op + trace) and ``finish``
(Doer-only explicit termination signal).

Neither tool touches the filesystem, memory, or any external service —
they exist purely to give the model an explicit signal channel and to
emit observability events the operator can audit later.
"""
from __future__ import annotations

from typing import Any

from ._trace import emit

_THOUGHT_MAX_BYTES = 4096
_SUMMARY_MAX_BYTES = 2048
_VALID_FINISH_STATUS = {"done", "blocked"}


def _clip(s: str, max_bytes: int) -> str:
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    suffix = "...[truncated]"
    budget = max_bytes - len(suffix)
    return s.encode("utf-8")[:budget].decode("utf-8", "replace") + suffix


def think(thought: str) -> dict[str, Any]:
    """Record an explicit reasoning step. Pure no-op for the model loop."""
    if not isinstance(thought, str):
        thought = str(thought)
    thought = _clip(thought, _THOUGHT_MAX_BYTES)
    emit("Think", {"thought": thought})
    return {"ok": True}


def finish(
    summary: str,
    status: str = "done",
    *,
    _agent_role: str | None = None,
) -> dict[str, Any]:
    """Doer-only explicit termination signal.

    Returns ``{ok: True, terminate: True, summary, status}`` on success.
    ADK's LoopAgent inspects ``terminate=True`` to halt the Doer step;
    the Feedback agent downstream reads ``summary`` and the last
    ``compile_status`` / ``test_status`` from session state.
    """
    if _agent_role is not None and _agent_role != "doer":
        return {"ok": False, "error": "agent_not_authorized",
                "role": _agent_role}
    if status not in _VALID_FINISH_STATUS:
        return {"ok": False, "error": "invalid_status", "status": status}
    if not isinstance(summary, str):
        summary = str(summary)
    summary = _clip(summary, _SUMMARY_MAX_BYTES)
    emit("Finish", {"summary": summary, "status": status})
    return {"ok": True, "terminate": True, "summary": summary, "status": status}
