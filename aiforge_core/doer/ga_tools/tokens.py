"""Token telemetry — running totals per ticket per role.

Aider's `/tokens` analogue. Lightweight in-process counter that
:func:`note` is called from the doer/feedback/learner LM call
sites with (role, prompt_tokens, completion_tokens). HTTP API
:func:`snapshot_for_ticket` exposes totals for the Settings UI
badge / per-ticket dashboard.

Backed by a thread-safe dict keyed by ticket identifier. No DB
overhead — totals reset when the runtime restarts. Persistence is
out of scope for KISS; the existing ticket events table holds
durable records.
"""
from __future__ import annotations

import threading
from typing import Optional

_LOCK = threading.Lock()
_COUNTS: dict[str, dict[str, dict[str, int]]] = {}
# {ticket_id: {role: {prompt_tokens: int, completion_tokens: int, calls: int}}}


def note(ticket_id: Optional[str], role: str,
         prompt_tokens: int, completion_tokens: int) -> None:
    if not ticket_id:
        return
    with _LOCK:
        bucket = _COUNTS.setdefault(ticket_id, {})
        row = bucket.setdefault(role, {
            "prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
        })
        row["prompt_tokens"] += int(prompt_tokens or 0)
        row["completion_tokens"] += int(completion_tokens or 0)
        row["calls"] += 1


def snapshot_for_ticket(ticket_id: str) -> dict:
    with _LOCK:
        return {
            "ticket": ticket_id,
            "by_role": dict(_COUNTS.get(ticket_id, {})),
        }


def snapshot_all() -> dict:
    with _LOCK:
        return {tid: dict(roles) for tid, roles in _COUNTS.items()}


def reset(ticket_id: str) -> None:
    with _LOCK:
        _COUNTS.pop(ticket_id, None)
