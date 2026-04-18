"""SQLite-backed ticket + comment + audit storage.

Single-writer design. Tickets are persistent; audit is append-only.
File: `.paperclip/paperclip.db` (gitignored). Schema migrations handled
by `ensure_schema()` on every open.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# DESIGN.md §4 lifecycle states — a superset that captures the full flow.
# `created` → `planning` → `tests_writing` → `coding` → `verifying`
#   → `reviewing` → `mr_created` → `merged` / `escalated`.
TICKET_STATES = (
    "created",
    "planning",
    "tests_writing",
    "coding",
    "verifying",
    "reviewing",
    "mr_created",
    "merged",
    "escalated",
)


@dataclass
class Ticket:
    id: str
    title: str
    body: str
    state: str
    assignee: str
    created_at: float
    updated_at: float


@dataclass
class Comment:
    id: int
    ticket_id: str
    author: str          # role key (em / tester / sr_developer / sr_architect / human)
    body: str
    created_at: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    assignee    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL REFERENCES tickets(id),
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    event       TEXT NOT NULL,          -- create, comment, assign, transition, tool_call, budget, escalate
    actor       TEXT NOT NULL,          -- role or 'system' or 'human'
    data_json   TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_ticket    ON comments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_audit_ticket       ON audit(ticket_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee   ON tickets(assignee);
CREATE INDEX IF NOT EXISTS idx_tickets_state      ON tickets(state);
"""


class Store:
    """Tickets + comments + audit log in one SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    # ---------- tickets ----------
    def create_ticket(self, title: str, body: str, assignee: str, state: str = "created") -> Ticket:
        if state not in TICKET_STATES:
            raise ValueError(f"unknown state: {state}")
        ticket_id = f"TICKET-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self.txn() as c:
            c.execute(
                "INSERT INTO tickets(id,title,body,state,assignee,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (ticket_id, title, body, state, assignee, now, now),
            )
            c.execute(
                "INSERT INTO audit(ticket_id,event,actor,data_json,created_at) VALUES(?,?,?,?,?)",
                (ticket_id, "create", "human", json.dumps({"title": title, "assignee": assignee}), now),
            )
        return Ticket(ticket_id, title, body, state, assignee, now, now)

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return Ticket(**dict(row)) if row else None

    def list_tickets(self, state: str | None = None, assignee: str | None = None) -> list[Ticket]:
        sql = "SELECT * FROM tickets WHERE 1=1"
        args: list = []
        if state:
            sql += " AND state=?"
            args.append(state)
        if assignee:
            sql += " AND assignee=?"
            args.append(assignee)
        sql += " ORDER BY updated_at DESC"
        return [Ticket(**dict(r)) for r in self._conn.execute(sql, args).fetchall()]

    def transition(self, ticket_id: str, new_state: str, actor: str) -> None:
        if new_state not in TICKET_STATES:
            raise ValueError(f"unknown state: {new_state}")
        now = time.time()
        with self.txn() as c:
            cur = c.execute("SELECT state FROM tickets WHERE id=?", (ticket_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(ticket_id)
            old = row["state"]
            c.execute("UPDATE tickets SET state=?, updated_at=? WHERE id=?", (new_state, now, ticket_id))
            c.execute(
                "INSERT INTO audit(ticket_id,event,actor,data_json,created_at) VALUES(?,?,?,?,?)",
                (ticket_id, "transition", actor, json.dumps({"from": old, "to": new_state}), now),
            )

    def assign(self, ticket_id: str, new_assignee: str, actor: str) -> None:
        now = time.time()
        with self.txn() as c:
            cur = c.execute("SELECT assignee FROM tickets WHERE id=?", (ticket_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(ticket_id)
            old = row["assignee"]
            c.execute("UPDATE tickets SET assignee=?, updated_at=? WHERE id=?", (new_assignee, now, ticket_id))
            c.execute(
                "INSERT INTO audit(ticket_id,event,actor,data_json,created_at) VALUES(?,?,?,?,?)",
                (ticket_id, "assign", actor, json.dumps({"from": old, "to": new_assignee}), now),
            )

    # ---------- comments ----------
    def add_comment(self, ticket_id: str, author: str, body: str) -> Comment:
        if self._conn.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone() is None:
            raise KeyError(ticket_id)
        now = time.time()
        with self.txn() as c:
            cur = c.execute(
                "INSERT INTO comments(ticket_id,author,body,created_at) VALUES(?,?,?,?)",
                (ticket_id, author, body, now),
            )
            cid = cur.lastrowid
            c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
            c.execute(
                "INSERT INTO audit(ticket_id,event,actor,data_json,created_at) VALUES(?,?,?,?,?)",
                (ticket_id, "comment", author, json.dumps({"comment_id": cid, "length": len(body)}), now),
            )
        return Comment(id=cid, ticket_id=ticket_id, author=author, body=body, created_at=now)

    def list_comments(self, ticket_id: str) -> list[Comment]:
        rows = self._conn.execute(
            "SELECT * FROM comments WHERE ticket_id=? ORDER BY id ASC", (ticket_id,)
        ).fetchall()
        return [Comment(**dict(r)) for r in rows]

    # ---------- audit ----------
    def audit_event(self, ticket_id: str, event: str, actor: str, data: dict | None = None) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO audit(ticket_id,event,actor,data_json,created_at) VALUES(?,?,?,?,?)",
            (ticket_id, event, actor, json.dumps(data or {}), now),
        )

    def list_audit(self, ticket_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,ticket_id,event,actor,data_json,created_at FROM audit WHERE ticket_id=? ORDER BY id ASC",
            (ticket_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "ticket_id": r["ticket_id"],
                "event": r["event"],
                "actor": r["actor"],
                "data": json.loads(r["data_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
