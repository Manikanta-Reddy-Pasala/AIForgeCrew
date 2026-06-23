"""Per-session human-approval gate for chat runs.

When a tool call resolves to the ``ask`` policy (see ``tools.tool_policy``)
the chat loop pauses: it streams an ``approval`` event describing the
action (with a diff preview for edits), then BLOCKS on this registry until
the user clicks Approve/Reject in the UI — which POSTs to
``/api/chat/sessions/{id}/approve`` and calls :func:`resolve`. This is the
Cline / opencode "approve each step" interaction, built on the same
session-bound-registry pattern as :mod:`chat_cancel`.

Usage (in the run loop, on an ``ask`` tool):
    pid = chat_approve.request(session_id, tool, args, preview)
    yield {"type": "approval", "id": pid, "tool": ..., "preview": ...}
    decision = chat_approve.wait(session_id)      # blocks
    if decision["decision"] != "approve": skip the tool

From the endpoint:
    chat_approve.resolve(session_id, "approve")   # or "reject"
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "reject"   # default-deny if the wait times out
    note: str = ""
    seq: int = 0


_LOCK = threading.Lock()
_PENDING: dict[int, _Pending] = {}


def _timeout_s() -> float:
    try:
        return float(os.environ.get("AIFORGE_CHAT_APPROVAL_TIMEOUT_S", "900"))
    except ValueError:
        return 900.0


def request(session_id: int) -> int:
    """Open a fresh approval request for ``session_id``. Returns a sequence
    id the UI echoes back so a stale Approve can't resolve a newer request."""
    with _LOCK:
        prev = _PENDING.get(session_id)
        seq = (prev.seq + 1) if prev else 1
        _PENDING[session_id] = _Pending(seq=seq)
        return seq


def wait(session_id: int) -> dict:
    """Block until the user resolves (or timeout → reject). Returns
    ``{"decision": "approve"|"reject", "note": str}``."""
    with _LOCK:
        p = _PENDING.get(session_id)
    if p is None:
        return {"decision": "reject", "note": "no pending approval"}
    ok = p.event.wait(timeout=_timeout_s())
    if not ok:
        return {"decision": "reject", "note": "approval timed out"}
    return {"decision": p.decision, "note": p.note}


def resolve(session_id: int, decision: str, note: str = "",
            seq: int | None = None) -> bool:
    """Resolve the pending approval. Returns True if one was waiting.
    ``seq`` (if given) must match the open request — guards against a
    late click resolving a newer prompt."""
    with _LOCK:
        p = _PENDING.get(session_id)
        if p is None:
            return False
        if seq is not None and seq != p.seq:
            return False
        p.decision = "approve" if str(decision).lower() in (
            "approve", "approved", "yes", "ok", "allow") else "reject"
        p.note = note or ""
        p.event.set()
        return True


def cancel(session_id: int) -> None:
    """Force-reject any pending approval (used when the run is stopped)."""
    with _LOCK:
        p = _PENDING.get(session_id)
        if p is not None and not p.event.is_set():
            p.decision = "reject"
            p.note = "cancelled"
            p.event.set()


def finish(session_id: int) -> None:
    with _LOCK:
        _PENDING.pop(session_id, None)


__all__ = ["request", "wait", "resolve", "cancel", "finish"]
