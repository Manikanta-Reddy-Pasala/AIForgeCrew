"""Scopes of llm/model_wait: what cancels a wait, who hears its status, which
calls must not wait at all. Bound per thread / task (contextvars)."""
from __future__ import annotations

import contextlib
import contextvars
import threading
from typing import Any, Callable

from . import model_outage

_SHUTDOWN = threading.Event()

# Bound per thread/task: extra cancel sources, status sinks, the no-wait flag.
_SCOPES: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "aiforge_model_wait_scopes", default=())
_SINKS: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "aiforge_model_wait_sinks", default=())
_OPTIONAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aiforge_model_wait_optional", default=False)
# (on_wait, on_back) pairs: called when a wait for a DOWN model starts and ends
# — the chat frees its generation slot for the length of the wait.
_HOOKS: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "aiforge_model_wait_hooks", default=())

LLMRequestFailing = model_outage.LLMRequestFailing

#: Set on an exception whose wait already ran to its bound, so an outer layer
#: does not start a second full wait on top of it (one layer owns the wait).
WAITED_ATTR = "aiforge_llm_waited"


# ── scopes: cancel sources and status sinks ────────────────────────────────

@contextlib.contextmanager
def scope(cancel: "threading.Event | Callable[[], bool] | None" = None,
          reason: str = "cancelled"):
    """Cancel every model wait inside this block when ``cancel`` fires."""
    tok = _SCOPES.set(_SCOPES.get() + ((cancel, reason),))
    try:
        yield
    finally:
        _SCOPES.reset(tok)


@contextlib.contextmanager
def status_sink(fn: Callable[[dict], Any]):
    """``fn(status)`` receives every status change of waits inside the block."""
    tok = _SINKS.set(_SINKS.get() + (fn,))
    try:
        yield
    finally:
        _SINKS.reset(tok)


def bind_status_sink(fn: Callable[[dict], Any]) -> None:
    """Add ``fn`` to this thread's sinks for the rest of its life (a worker
    thread that has no ``with`` block around its call)."""
    _SINKS.set(_SINKS.get() + (fn,))


def bind_wait_hooks(on_wait: Callable[[], Any],
                    on_back: Callable[[], Any]) -> None:
    """For the rest of this thread: ``on_wait()`` when a wait for a down model
    starts, ``on_back()`` when it ends (back, cancelled or given up)."""
    _HOOKS.set(_HOOKS.get() + ((on_wait, on_back),))


def was_waited(exc: BaseException | None) -> bool:
    """Did a layer below already wait out this failure to its bound?"""
    return any(getattr(e, WAITED_ATTR, False) for e in model_outage.chain(exc))


@contextlib.contextmanager
def optional():
    """Calls inside do not wait for the model — they fail at once."""
    tok = _OPTIONAL.set(True)
    try:
        yield
    finally:
        _OPTIONAL.reset(tok)


def scoped(fn: Callable, cancel=None, reason: str = "stopped") -> Callable:
    """``fn`` wrapped to run under :func:`scope` — for a worker-pool thread,
    which does not inherit the submitting thread's context (so neither its
    Stop nor its scopes): pass the cancel check explicitly."""
    def _run(*a, **k):
        with scope(cancel, reason):
            return fn(*a, **k)
    return _run


def side_call(fn: Callable) -> Callable:
    """``fn`` wrapped to run under :func:`optional` — for a bounded side call
    handed to a pool thread (a classifier with a 6 s budget, a curator): it is
    abandoned at its budget, so it must fail at once on an outage instead of
    waiting on in a leaked thread and firing late when the model returns."""
    def _run(*a, **k):
        with optional():
            return fn(*a, **k)
    return _run


def shutdown() -> None:
    """Process is exiting: every wait ends with :class:`ModelWaitCancelled`."""
    _SHUTDOWN.set()


def _reset_for_tests() -> None:
    _SHUTDOWN.clear()


def _fired(src) -> bool:
    try:
        if src is None:
            return False
        if hasattr(src, "is_set"):
            return bool(src.is_set())
        return bool(src())
    except Exception:  # noqa: BLE001 — a broken check never cancels
        return False


def cancel_reason() -> str | None:
    """Why the wait must end now, or None to keep waiting."""
    if _SHUTDOWN.is_set():
        return "shutting down"
    for src, why in _SCOPES.get():
        if _fired(src):
            return why
    try:
        from aiforge_core.llm.client._http import _CANCEL
        if _fired(_CANCEL.get()):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import run_interrupt
        if _fired(run_interrupt._stop_event.get()):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
        if sid is not None and chat_cancel.is_cancelled(sid):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    return None

