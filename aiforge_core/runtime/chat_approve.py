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

# Per-session "review edits" flag (Gap D). When True, EVERY file-mutating
# tool call is held for human Approve/Reject (with a real diff preview) before
# it lands — even when the permission policy would auto-allow it. Opt-in per
# run via the chat message body; default off → behavior unchanged. Cleared on
# :func:`finish` alongside the pending-approval and emitter state so it can't
# leak into the next turn.
_REVIEW_EDITS: dict[int, bool] = {}


def set_review_edits(session_id: int | None, on: bool) -> None:
    """Turn the pre-apply review gate on/off for ``session_id``."""
    if session_id is None:
        return
    with _LOCK:
        if on:
            _REVIEW_EDITS[session_id] = True
        else:
            _REVIEW_EDITS.pop(session_id, None)


def review_edits(session_id: int | None) -> bool:
    """True if the pre-apply review gate is armed for ``session_id``."""
    if session_id is None:
        return False
    with _LOCK:
        return bool(_REVIEW_EDITS.get(session_id))


# Per-session CHAT MODE (simple/plan/team). Recorded at run start so the tool
# gate — which only has the session id — can consult the per-mode approval
# setting (config.approval_settings). Cleared on :func:`finish`.
_MODE: dict[int, str] = {}


def set_mode(session_id: int | None, mode: str) -> None:
    """Record this run's chat mode for ``session_id`` (None → no-op)."""
    if session_id is None:
        return
    with _LOCK:
        _MODE[session_id] = (mode or "").strip().lower() or "simple"


def get_mode(session_id: int | None) -> str:
    if session_id is None:
        return ""
    with _LOCK:
        return _MODE.get(session_id, "")


def approvals_required(session_id: int | None) -> bool:
    """Whether this session's mode requires human approval (per the per-mode
    Settings toggle). Defaults ON — an unknown session or any lookup failure
    fails safe to requiring approval. Autonomous runs (id None) return True but
    the gates independently no-op without a human approver."""
    if session_id is None:
        return True
    try:
        from aiforge_core.config import approval_settings
        return approval_settings.required(get_mode(session_id))
    except Exception:  # noqa: BLE001
        return True

# Per-session event emitter. The simple chat loop yields approval events
# itself; the TEAM pipeline runs in a background thread whose tool-gate
# callback can't yield — it pushes the approval event through this emitter
# (registered by the pipeline driver to enqueue onto its SSE queue).
_EMITTERS: dict[int, object] = {}


def set_emitter(session_id: int, fn) -> None:
    with _LOCK:
        _EMITTERS[session_id] = fn


def clear_emitter(session_id: int) -> None:
    with _LOCK:
        _EMITTERS.pop(session_id, None)


def has_emitter(session_id: int | None) -> bool:
    if session_id is None:
        return False
    with _LOCK:
        return session_id in _EMITTERS


def emit(session_id: int, event: dict) -> bool:
    """Push an event to the session's registered emitter (the chat stream).
    Returns True if delivered. No-op when none registered."""
    with _LOCK:
        fn = _EMITTERS.get(session_id)
    if fn is None:
        return False
    try:
        fn(event)
        return True
    except Exception:  # noqa: BLE001
        return False


def _timeout_s() -> float:
    try:
        return float(os.environ.get("AIFORGE_CHAT_APPROVAL_TIMEOUT_S", "900"))
    except ValueError:
        return 900.0


def request(session_id: int) -> int:
    """Open a fresh approval request for ``session_id``. Returns a sequence
    id the UI echoes back so a stale Approve can't resolve a newer request.

    If a prior request is still pending (a waiter blocked on it), force-reject
    it first so that waiter unblocks instead of hanging to its timeout."""
    with _LOCK:
        prev = _PENDING.get(session_id)
        seq = (prev.seq + 1) if prev else 1
        if prev is not None and not prev.event.is_set():
            prev.decision = "reject"
            prev.note = "superseded"
            prev.event.set()
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
    # Defensively unblock any in-flight waiter (decision stays default-reject)
    # so a run that ends/aborts while a tool is awaiting approval doesn't
    # leave an executor thread blocked until the 900s timeout.
    with _LOCK:
        p = _PENDING.pop(session_id, None)
        _REVIEW_EDITS.pop(session_id, None)   # no stale review flag next turn
        _MODE.pop(session_id, None)           # no stale mode next turn
    if p is not None and not p.event.is_set():
        p.decision = "reject"
        p.note = "run finished"
        p.event.set()


__all__ = ["request", "wait", "resolve", "cancel", "finish",
           "set_emitter", "clear_emitter", "has_emitter", "emit",
           "set_review_edits", "review_edits",
           "set_mode", "get_mode", "approvals_required"]
