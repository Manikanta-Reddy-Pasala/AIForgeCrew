"""Shared trace-event emitter for the OpenHands-parity tool surface.

Every tool calls :func:`emit` to record a labelled event on the Neo4j
trace alongside the existing ``:Turn`` / ``:ToolCall`` nodes. The emit
path is best-effort: a Neo4j outage or missing config never bubbles into
the model loop. Oversized string fields are truncated to 4 KB to keep
the audit trail bounded.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("aiforge.tools.trace")

_MAX_STR_BYTES = 4096
_TRUNC_SUFFIX = "...[truncated]"


def _clip(val: Any) -> Any:
    if isinstance(val, str) and len(val.encode("utf-8")) > _MAX_STR_BYTES:
        budget = _MAX_STR_BYTES - len(_TRUNC_SUFFIX)
        return val.encode("utf-8")[:budget].decode("utf-8", "replace") + _TRUNC_SUFFIX
    return val


def _safe_emit(label: str, props: dict[str, Any]) -> None:
    """Delegate to runtime.observability.emit_trace. Importing here keeps
    the module unit-test friendly (test_trace.py mocks this function)."""
    from aiforge_core.runtime import observability

    fn = getattr(observability, "emit_trace", None)
    if fn is None:
        return
    fn(label=label, props=props)


def emit(label: str, props: dict[str, Any]) -> None:
    """Record a trace event. Best-effort: never raises into the agent loop."""
    clean: dict[str, Any] = {"ts": time.time()}
    for k, v in props.items():
        clean[k] = _clip(v)
    try:
        _safe_emit(label, clean)
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log.debug("trace.emit_failed label=%s: %s", label, exc)
