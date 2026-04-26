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
    try:
        import psycopg
        from .config import AIFORGE_DSN
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2) as c, \
             c.cursor() as cur:
            cur.execute(
                "UPDATE hitl_pending SET resolved_at = now() "
                "WHERE id = %s",
                (pending_id,),
            )
            c.commit()
    except Exception:
        pass


def list_pending(*, ticket: str | None = None) -> list[dict]:
    """Return open HITL requests. Used by the dispatcher poll."""
    out: list[dict] = []
    try:
        import psycopg
        from .config import AIFORGE_DSN
        sql = (
            "SELECT id, ticket, message, snapshot, "
            "       extract(epoch from created_at) AS created_epoch "
            "  FROM hitl_pending "
            " WHERE resolved_at IS NULL "
            + ("  AND ticket = %s " if ticket else "")
            + " ORDER BY created_at"
        )
        params = (ticket,) if ticket else ()
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2) as c, \
             c.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                out.append({
                    "id": row[0], "ticket": row[1], "message": row[2],
                    "snapshot": row[3] or {},
                    "created_at": float(row[4]),
                })
    except Exception:
        pass
    return out


def resume(
    pending_id: str,
    *,
    runner: object | None = None,
) -> dict:
    """Resume a parked HITL request once an answer landed.

    Workflow:
      1. Look up answer via ``find_answer(ticket, ...)``.
      2. If present, hand the snapshot + answer to ``runner`` callback
         and mark the row resolved.
      3. If absent, return ``{status: "waiting"}`` so the dispatcher
         can poll again next tick.

    Caller-provided ``runner(snapshot, answer) -> outcome`` is the
    re-entry point — typically ``run_doer_via_ga`` or
    ``run_planner_via_ga`` rebuilt from the stashed snapshot.
    KISS: this module only sequences; doesn't know about GA internals.
    """
    pending = _PENDING.get(pending_id)
    if pending is None:
        # Hot-cache miss — try Postgres.
        for row in list_pending():
            if row["id"] == pending_id:
                pending = Pending(
                    id=row["id"], ticket=row["ticket"],
                    message=row["message"],
                    created_at=row["created_at"],
                    snapshot=row["snapshot"],
                )
                _PENDING[pending_id] = pending
                break
    if pending is None:
        return {"status": "unknown_id"}

    answer = find_answer(pending.ticket)
    if answer is None:
        return {"status": "waiting", "ticket": pending.ticket}

    if runner is None:
        return {"status": "ready",
                "ticket": pending.ticket,
                "answer": answer,
                "snapshot": pending.snapshot or {}}

    try:
        outcome = runner(pending.snapshot or {}, answer)
    except Exception as exc:
        return {"status": "runner_error", "err": str(exc)[:300]}
    mark_resolved(pending_id)
    return {"status": "resumed", "outcome": outcome}


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
