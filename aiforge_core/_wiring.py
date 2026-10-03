"""Composition root: plug the runtime into the hooks the LLM layer exposes.

Imported once by ``aiforge_core/__init__`` so it runs for every entry point
(API, CLI, a test importing only ``aiforge_core.llm``). Each function imports
its runtime module on call, so importing this file costs nothing and a test
that replaces a runtime function still takes effect.
"""
from __future__ import annotations

from aiforge_core.llm import hooks


def _is_foreground_active() -> bool:
    from aiforge_core.runtime import chat_runs
    return chat_runs.any_active()


def _context_session_id():
    from aiforge_core.runtime import request_context
    return request_context.context_session_id()


def _session_id():
    from aiforge_core.runtime import request_context
    return request_context.get_session_id()


def _role():
    from aiforge_core.runtime import request_context
    return request_context.get_role()


def _stop_requested() -> bool:
    from aiforge_core.llm._wait_scope import fired
    try:
        from aiforge_core.runtime import run_interrupt
        if fired(run_interrupt._stop_event.get()):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
        if sid is not None and chat_cancel.is_cancelled(sid):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _record_perf(family: str, name: str, ms: float) -> None:
    from aiforge_core.runtime import perf_recorder
    perf_recorder.record(family, name, ms)


def _timed_perf(family: str, name: str):
    from aiforge_core.runtime import perf_recorder
    return perf_recorder.timed(family, name)


hooks.install(
    is_foreground_active=_is_foreground_active,
    context_session_id=_context_session_id,
    session_id=_session_id,
    role=_role,
    stop_requested=_stop_requested,
    record_perf=_record_perf,
    timed_perf=_timed_perf,
)
