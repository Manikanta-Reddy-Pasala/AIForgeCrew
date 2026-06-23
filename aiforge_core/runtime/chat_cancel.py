"""Per-session cancellation for chat runs.

A chat turn (simple ReAct agent OR the full ADK team pipeline) runs
server-side, often spawning subprocesses (builds, test runs) and, in team
mode, a background thread driving the agent graph. Closing the browser
stream alone does NOT stop that work. This registry lets the Stop button
(``POST /api/chat/sessions/{id}/stop``) signal the run to halt AND kill
any subprocess groups it started.

Usage:
    token = chat_cancel.start(session_id)
    ...                               # in the run loop: if token.cancelled: break
    chat_cancel.track_pgid(session_id, os.getpgid(proc.pid))
    ...
    chat_cancel.finish(session_id)    # in finally

    chat_cancel.cancel(session_id)    # from the /stop endpoint
"""
from __future__ import annotations

import contextvars
import os
import signal
import threading
from dataclasses import dataclass, field

# The session a run loop / its tools belong to. Set at the top of the run
# (and re-set inside the team-mode driver thread) so run_command / bash can
# register their subprocess groups + poll for cancellation without an
# explicit param threaded through every call site.
_active: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "chat_session", default=None)


def set_active(session_id: int | None) -> None:
    _active.set(session_id)


def active() -> int | None:
    return _active.get()


@dataclass
class _Token:
    event: threading.Event = field(default_factory=threading.Event)
    pgids: set[int] = field(default_factory=set)

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


_LOCK = threading.Lock()
_RUNS: dict[int, _Token] = {}


def start(session_id: int) -> _Token:
    """Begin a cancellable run for ``session_id`` (replaces any prior)."""
    with _LOCK:
        tok = _Token()
        _RUNS[session_id] = tok
        return tok


def get(session_id: int) -> _Token | None:
    with _LOCK:
        return _RUNS.get(session_id)


def is_cancelled(session_id: int) -> bool:
    tok = get(session_id)
    return bool(tok and tok.cancelled)


def track_pgid(session_id: int, pgid: int) -> None:
    """Record a subprocess process-group id so cancel() can kill it."""
    tok = get(session_id)
    if tok is not None and pgid > 0:
        with _LOCK:
            tok.pgids.add(pgid)


def cancel(session_id: int) -> bool:
    """Signal the run to stop and SIGTERM/SIGKILL its subprocess groups.

    Returns True if a run was active.
    """
    tok = get(session_id)
    if tok is None:
        return False
    tok.event.set()
    for pgid in list(tok.pgids):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return True


def finish(session_id: int) -> None:
    with _LOCK:
        _RUNS.pop(session_id, None)
