"""What the LLM layer needs from the layer above it, as plain callables.

``llm`` sits below ``runtime``, so it must not import it. The few facts it
wants from the running system — is a chat turn in flight, which session and
role is this call for, has the operator pressed Stop, where do timings go —
are module-level functions here. Each defaults to "nothing known"; the
composition root (``aiforge_core/_wiring.py``, run when ``aiforge_core`` is
imported) replaces them once with the runtime-backed versions. Callers use
``hooks.name()`` so a replaced function takes effect everywhere.
"""
from __future__ import annotations

import contextlib


def is_foreground_active() -> bool:
    """True while a chat run is in flight."""
    return False


def context_session_id():
    """Session bound to THIS context only (no process-wide fallback)."""
    return None


def session_id():
    """Active session id, with the process-wide fallback; None if unknown."""
    return None


def role():
    """Agent role bound to this context; None if unknown."""
    return None


def stop_requested() -> bool:
    """True when the operator stopped the running turn."""
    return False


def record_perf(family: str, name: str, ms: float) -> None:
    """One timing sample, in milliseconds."""


def timed_perf(family: str, name: str):
    """Context manager recording the elapsed time of its block."""
    return contextlib.nullcontext()


def install(**fns) -> None:
    """Replace hooks by name (``install(record_perf=fn)``)."""
    g = globals()
    for name, fn in fns.items():
        if name.startswith("_") or not callable(g.get(name)) or name == "install":
            raise AttributeError(f"no such llm hook: {name}")
        g[name] = fn
