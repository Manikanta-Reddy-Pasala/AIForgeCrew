"""Ticket + ticket_event CRUD on aiforge Postgres.

Source of truth for work items.

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


_DANGEROUS_PATTERNS = [
    "drop table", "rm -rf /", "rm -rf ~", "delete all", "truncate table",
    "shutdown -h", "mkfs", "format c:", "> /dev/sda",
]
_URGENT_KEYWORDS = ["prod", "outage", "crash", "p0", "urgent", "incident"]


def _apply_supervisor_invariants(
    title: str, body: str, assignee_role: str | None,
    priority: str, labels: list[str] | None, metadata: dict | None,
) -> tuple[str | None, str, list[str], dict]:
    """Enforce hard safety + routing invariants at ticket-create time.
    Supervisor LLM still runs for the creative decisions; these are the
    floor that prevents dangerous work from being auto-routed."""
    labels = list(labels or [])
    metadata = dict(metadata or {})
    lower_body = f"{title}\n{body}".lower()

    # Dangerous intent → force supervisor review, never auto-route.
    if any(pat in lower_body for pat in _DANGEROUS_PATTERNS):
        assignee_role = "supervisor"
        if "review-required" not in labels:
            labels.append("review-required")
        metadata["dangerous_pattern"] = True

    # Auto priority-boost on urgent keywords.
    if priority not in ("urgent",) and any(kw in lower_body for kw in _URGENT_KEYWORDS):
        priority = "urgent"
        metadata["priority_auto_boosted"] = True

    # Default assignee → supervisor for triage.
    if assignee_role is None:
        assignee_role = "supervisor"

    return assignee_role, priority, labels, metadata


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
    # Children inherit their parent's assignee if caller didn't pick — DON'T
    # send them through supervisor triage again.
    if parent_id is None:
        assignee_role, priority, labels, metadata = _apply_supervisor_invariants(
            title, body, assignee_role, priority, labels, metadata,
        )
    else:
        labels = list(labels or [])
        metadata = dict(metadata or {})
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
             json.dumps(metadata)),
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


_ROLE_ALIASES = {
    "supervisor":  ["supervisor", "architect"],
    "planner":     ["planner", "sr_developer"],
    "doer":        ["doer", "developer"],
    "learner":     ["learner", "fact_extract"],
    "feedback":    ["feedback"],
}


def _aliases_for(role: str) -> list[str]:
    """Return canonical role + any legacy names that should also match."""
    if role in _ROLE_ALIASES:
        return _ROLE_ALIASES[role]
    for canonical, aliases in _ROLE_ALIASES.items():
        if role in aliases:
            return aliases
    return [role]


def claim_next(role: str) -> Ticket | None:
    """Atomically pick + mark-in_progress the next todo ticket for a role.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so two parallel tick processes
    can't claim the same row. Marks status='in_progress' inside the same
    transaction, so by the time this returns the ticket is ours.

    Matches canonical + legacy role names via _aliases_for.

    Returns None when no todo exists for this role (caller should emit
    tick.idle and exit)."""
    aliases = _aliases_for(role)
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM tickets "
            "WHERE assignee_role = ANY(%s) AND status='todo' "
            "ORDER BY CASE priority "
            "  WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "created_at ASC LIMIT 1 "
            "FOR UPDATE SKIP LOCKED",
            (aliases,),
        )
        row = cur.fetchone()
        if row is None:
            c.rollback()
            return None
        # Atomic claim: flip to in_progress before releasing the row lock.
        cur.execute(
            "UPDATE tickets SET status='in_progress' WHERE id=%s RETURNING *",
            (row["id"],),
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO ticket_events (ticket_id, agent_role, kind, body) "
            "VALUES (%s, %s, 'status_change', 'in_progress')",
            (row["id"], role),
        )
        c.commit()
    return Ticket.from_row(row)


def claim_next_any() -> Ticket | None:
    """Atomically claim the oldest todo ticket across all roles.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent graph runners
    cannot double-claim. Marks status='in_progress' in the same transaction.
    Returns None when no todo tickets exist for any role.
    """
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM tickets "
            "WHERE status='todo' "
            "ORDER BY CASE priority "
            "  WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "created_at ASC LIMIT 1 "
            "FOR UPDATE SKIP LOCKED",
        )
        row = cur.fetchone()
        if row is None:
            c.rollback()
            return None
        cur.execute(
            "UPDATE tickets SET status='in_progress' WHERE id=%s RETURNING *",
            (row["id"],),
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO ticket_events (ticket_id, agent_role, kind, body) "
            "VALUES (%s, %s, 'status_change', 'in_progress')",
            (row["id"], "graph_runner"),
        )
        c.commit()
    return Ticket.from_row(row)


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


def by_title_project(title: str, project: str | None) -> list[Ticket]:
    """Find tickets with same lower(title) in the same project (active only)."""
    if not title:
        return []
    needle = title.strip().lower()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        if project:
            cur.execute(
                "SELECT * FROM tickets "
                "WHERE lower(title)=%s AND project=%s "
                "AND status IN ('todo','in_progress','in_review','blocked','done') "
                "ORDER BY created_at ASC LIMIT 20",
                (needle, project),
            )
        else:
            cur.execute(
                "SELECT * FROM tickets WHERE lower(title)=%s "
                "AND status IN ('todo','in_progress','in_review','blocked','done') "
                "ORDER BY created_at ASC LIMIT 20",
                (needle,),
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
