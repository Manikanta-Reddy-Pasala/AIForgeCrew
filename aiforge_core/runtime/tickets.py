"""Ticket + ticket_event CRUD on aiforge Postgres.

Replaces Paperclip as the source of truth for work items.

Public surface:
    new_identifier()                 -> str            # atomic ONE-<n>
    create(title, body, ...)         -> Ticket
    get(identifier | id)             -> Ticket
    claim_next(role)                 -> Ticket | None  # oldest todo for role
    update_status(id, status, ...)   -> Ticket
    add_comment(id, role, body)      -> int
    add_event(id, role, kind, body, metadata) -> int
    children(parent_id)              -> list[Ticket]
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import AIFORGE_DSN


VALID_STATUS = {"todo", "in_progress", "in_review", "done", "blocked", "cancelled"}
VALID_PRIORITY = {"low", "medium", "high", "urgent"}


@dataclass
class Ticket:
    id: int
    identifier: str
    title: str
    body: str
    status: str
    priority: str
    assignee_role: str | None
    parent_id: int | None
    branch: str | None
    project: str | None
    labels: list[str]
    metadata: dict
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_row(cls, r: dict) -> "Ticket":
        return cls(
            id=r["id"], identifier=r["identifier"], title=r["title"], body=r["body"],
            status=r["status"], priority=r["priority"],
            assignee_role=r["assignee_role"], parent_id=r["parent_id"],
            branch=r["branch"], project=r["project"],
            labels=list(r["labels"] or []), metadata=dict(r["metadata"] or {}),
            created_at=r["created_at"], updated_at=r["updated_at"],
            completed_at=r["completed_at"],
        )


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    c = psycopg.connect(AIFORGE_DSN, autocommit=False, connect_timeout=5,
                        options="-c statement_timeout=15000")
    try:
        yield c
    finally:
        c.close()


def new_identifier() -> str:
    """Atomic ONE-<n> allocator. Uses the singleton ticket_counter row."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ticket_counter SET next_n = next_n + 1 WHERE singleton "
            "RETURNING next_n - 1"
        )
        n = cur.fetchone()[0]
        c.commit()
    return f"ONE-{n}"


def create(
    *,
    title: str,
    body: str = "",
    assignee_role: str | None = None,
    parent_id: int | None = None,
    priority: str = "medium",
    project: str | None = None,
    labels: list[str] | None = None,
    branch: str | None = None,
    metadata: dict | None = None,
    identifier: str | None = None,
) -> Ticket:
    if priority not in VALID_PRIORITY:
        raise ValueError(f"bad priority {priority!r}")
    ident = identifier or new_identifier()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO tickets
              (identifier, title, body, priority, assignee_role,
               parent_id, project, labels, branch, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            RETURNING *;
            """,
            (ident, title, body, priority, assignee_role,
             parent_id, project, labels or [], branch,
             json.dumps(metadata or {})),
        )
        row = cur.fetchone()
        c.commit()
    return Ticket.from_row(row)


def get(ident_or_id: str | int) -> Ticket | None:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        if isinstance(ident_or_id, int):
            cur.execute("SELECT * FROM tickets WHERE id=%s", (ident_or_id,))
        else:
            cur.execute("SELECT * FROM tickets WHERE identifier=%s", (ident_or_id,))
        row = cur.fetchone()
    return Ticket.from_row(row) if row else None


def claim_next(role: str) -> Ticket | None:
    """Pick the next todo ticket for a role: priority DESC, then FIFO.
    Does NOT mutate status — caller must update_status(..., 'in_progress')
    once it actually starts executing."""
    priority_rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM tickets "
            "WHERE assignee_role=%s AND status='todo' "
            "ORDER BY CASE priority "
            "  WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "created_at ASC LIMIT 1",
            (role,),
        )
        row = cur.fetchone()
    return Ticket.from_row(row) if row else None


def update_status(ticket_id: int, status: str, *, role: str | None = None,
                  metadata_patch: dict | None = None) -> Ticket:
    if status not in VALID_STATUS:
        raise ValueError(f"bad status {status!r}")
    completed_at = "now()" if status in ("done", "cancelled") else "completed_at"
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE tickets SET status=%s, completed_at={completed_at}, "
            "metadata = metadata || %s::jsonb WHERE id=%s RETURNING *;",
            (status, json.dumps(metadata_patch or {}), ticket_id),
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO ticket_events (ticket_id, agent_role, kind, body) "
            "VALUES (%s, %s, 'status_change', %s)",
            (ticket_id, role, status),
        )
        c.commit()
    return Ticket.from_row(row)


def add_comment(ticket_id: int, role: str | None, body: str,
                metadata: dict | None = None) -> int:
    return add_event(ticket_id, role, "comment", body, metadata)


def add_event(ticket_id: int, role: str | None, kind: str, body: str | None,
              metadata: dict | None = None) -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ticket_events (ticket_id, agent_role, kind, body, metadata) "
            "VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (ticket_id, role, kind, body, json.dumps(metadata or {})),
        )
        eid = cur.fetchone()[0]
        c.commit()
    return eid


def children(parent_id: int) -> list[Ticket]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM tickets WHERE parent_id=%s ORDER BY created_at ASC",
            (parent_id,),
        )
        rows = cur.fetchall()
    return [Ticket.from_row(r) for r in rows]


def comments(ticket_id: int, limit: int = 50) -> list[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT created_at, agent_role, kind, body, metadata "
            "FROM ticket_events WHERE ticket_id=%s "
            "ORDER BY created_at ASC LIMIT %s",
            (ticket_id, limit),
        )
        return list(cur.fetchall())
