"""Shared trace-event emitter for the OpenHands-parity tool surface.

Every tool calls :func:`emit` to record a labelled event on the
``aiforge.tools.trace`` logger at DEBUG, so the trail is only written when
that logger is set to DEBUG (``aiforge`` runs at INFO). The emit path is best-effort: a
sink outage or missing config never bubbles into the model loop.
Oversized string fields are truncated to 4 KB to keep the audit trail
bounded.
"""
from __future__ import annotations

import json
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
    """Write the event to the trace logger. It used to look up an
    ``observability.emit_trace`` that never existed, so every event was
    dropped (test_trace.py mocks this function)."""
    if log.isEnabledFor(logging.DEBUG):
        log.debug("trace %s %s", label, json.dumps(props, default=str))


def emit(label: str, props: dict[str, Any]) -> None:
    """Record a trace event. Best-effort: never raises into the agent loop."""
    clean: dict[str, Any] = {"ts": time.time()}
    for k, v in props.items():
        clean[k] = _clip(v)
    try:
        _safe_emit(label, clean)
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log.debug("trace.emit_failed label=%s: %s", label, exc)
