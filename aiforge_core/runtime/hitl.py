"""HITL request_input_rerun stub — ADK 2.0 ``RequestInput`` shape.

KISS: when the Doer / Planner needs operator input mid-flight, it
calls :func:`request_input(message, ticket)`. We persist a pending
input record + post a ticket comment and return a ``REQUEST_INPUT``
sentinel that the orchestrator interprets as "park this run, resume
when operator answers".

Resume path:
1. Operator replies on the ticket with ``aiforge:answer:<text>``.
2. Webhook (or poller) writes the answer into the pending row.
3. Next dispatcher tick spots the row, reconstructs the GA session
   from the snapshot, injects the answer as the next user prompt,
   resumes ``agent_runner_loop``.

Until ADK 2.0 sidecar lands, the resume path is a STUB — we only
record + park. The replay/resume becomes a thin port of the helpers
in ``google.adk.workflow.utils._workflow_hitl_utils`` once we
integrate ADK 2.0 ``Workflow``.

Public surface:
- ``request_input(message, *, ticket, snapshot=None) -> Pending``
- ``find_answer(ticket, *, age_max_s=86400) -> str | None``
- ``mark_resolved(pending_id) -> None``
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Pending:
    id: str
    ticket: str
    message: str
    created_at: float
    snapshot: dict | None = None


_PENDING: dict[str, Pending] = {}


def request_input(
    message: str, *, ticket: str, snapshot: dict | None = None,
) -> Pending:
    """Park the run, post a ticket comment, return Pending sentinel."""
    pid = f"hitl-{ticket}-{int(time.time() * 1000)}"
    pending = Pending(
        id=pid, ticket=ticket, message=message,
        created_at=time.time(), snapshot=snapshot,
    )
    _PENDING[pid] = pending
    _post_ticket_comment(ticket, message, pid)
    _persist(pending)
    return pending


def find_answer(ticket: str, *, age_max_s: int = 86400) -> str | None:
    """Look for an ``aiforge:answer:<text>`` reply on the ticket.

    Returns the answer text or None when no fresh reply exists.
    """
    try:
        from aiforge_core.runtime import tickets as _tk
        events = _tk.list_events(ticket, limit=20)
    except Exception:
        return None
    cutoff = time.time() - age_max_s
    for ev in events:
        ts = float(getattr(ev, "created_at_epoch", 0) or 0)
        if ts < cutoff:
            continue
        body = (getattr(ev, "body", "") or "").strip()
        if body.startswith("aiforge:answer:"):
            return body[len("aiforge:answer:"):].strip()
    return None


def mark_resolved(pending_id: str) -> None:
    _PENDING.pop(pending_id, None)


# ───────── helpers ────────────────────────────────────────────────


def _post_ticket_comment(ticket: str, message: str, pending_id: str) -> None:
    try:
        from aiforge_core.runtime import tickets as _tk
        body = (
            f"[HITL · pending {pending_id}]\n\n{message}\n\n"
            f"Reply with: `aiforge:answer:<your answer>`"
        )
        _tk.add_event(
            _tk.id_for(ticket), "doer", "hitl_request",
            body=body[:4000],
            metadata={"pending_id": pending_id},
        )
    except Exception as exc:
        print(f"[hitl] ticket comment failed: {exc}")


def _persist(pending: Pending) -> None:
    """Best-effort Postgres write — survives orchestrator restart."""
    try:
        import psycopg
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2) as c, \
             c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS hitl_pending ("
                " id TEXT PRIMARY KEY,"
                " ticket TEXT,"
                " message TEXT,"
                " snapshot JSONB,"
                " created_at TIMESTAMPTZ DEFAULT now(),"
                " resolved_at TIMESTAMPTZ)"
            )
            cur.execute(
                "INSERT INTO hitl_pending(id, ticket, message, snapshot)"
                " VALUES (%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (id) DO NOTHING",
                (pending.id, pending.ticket, pending.message,
                 json.dumps(pending.snapshot or {})),
            )
            c.commit()
    except Exception as exc:
        print(f"[hitl] persist failed: {exc}")
