"""SQLite implementation of StoreBackend — zero-infra default.

JSON columns (labels, metadata) are stored as TEXT and (de)serialized
here. Timestamps are ISO-8601 TEXT via strftime. Identifiers come from
a single-row counter table updated atomically.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier      TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'todo',
    priority        TEXT NOT NULL DEFAULT 'medium',
    assignee_role   TEXT,
    parent_id       INTEGER,
    branch          TEXT,
    project         TEXT,
    labels          TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{}',
    route           TEXT NOT NULL DEFAULT 'code',
    route_workflow  TEXT,
    route_source    TEXT NOT NULL DEFAULT 'auto',
    route_confidence REAL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS tickets_assignee_status ON tickets(assignee_role, status);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(parent_id);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS tickets_route ON tickets(route, route_workflow);

CREATE TABLE IF NOT EXISTS ticket_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL,
    role        TEXT,
    kind        TEXT NOT NULL,
    body        TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ticket_events_ticket_ts ON ticket_events(ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ticket_events_kind ON ticket_events(kind);

CREATE TABLE IF NOT EXISTS ticket_counter (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);
"""


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["labels"] = json.loads(d.get("labels") or "[]")
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    for k in ("created_at", "updated_at", "completed_at"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d


class SqliteBackend:
    name = "sqlite"

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_DDL)

    def new_identifier(self) -> str:
        with _LOCK, self._conn() as c:
            c.execute(
                "INSERT INTO ticket_counter(id, value) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET value = value + 1"
            )
            n = c.execute("SELECT value FROM ticket_counter WHERE id = 1").fetchone()[0]
        return f"ONE-{n}"

    def create(self, fields: dict) -> dict:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO tickets
                  (identifier, title, body, status, priority, assignee_role,
                   parent_id, branch, project, labels, metadata,
                   route, route_workflow, route_source, route_confidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fields["identifier"], fields["title"], fields.get("body", ""),
                    fields.get("status", "todo"), fields.get("priority", "medium"),
                    fields.get("assignee_role"), fields.get("parent_id"),
                    fields.get("branch"), fields.get("project"),
                    json.dumps(fields.get("labels") or []),
                    json.dumps(fields.get("metadata") or {}),
                    fields.get("route", "code"), fields.get("route_workflow"),
                    fields.get("route_source", "auto"), fields.get("route_confidence"),
                ),
            )
            new_id = cur.lastrowid
            r = c.execute("SELECT * FROM tickets WHERE id = ?", (new_id,)).fetchone()
        return _row_to_dict(r)

    def get(self, ident_or_id) -> "dict | None":
        with self._conn() as c:
            if isinstance(ident_or_id, int) or str(ident_or_id).isdigit():
                r = c.execute("SELECT * FROM tickets WHERE id = ?",
                              (int(ident_or_id),)).fetchone()
            else:
                r = c.execute("SELECT * FROM tickets WHERE identifier = ?",
                              (str(ident_or_id),)).fetchone()
        return _row_to_dict(r) if r else None
