"""SQLite implementation of StoreBackend — zero-infra default.

Raw dialect-specific ops only; all business logic lives in store.py.
JSON columns (labels, metadata) are stored as TEXT and (de)serialized
here so returned rows match the psycopg dict_row shape (labels -> list,
metadata -> dict, timestamps -> datetime). The counter is seeded at 100
so the first identifier is ONE-100, matching the historical Postgres
behavior.
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

_PRIORITY_ORDER = (
    "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_DDL = f"""
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier      TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'todo',
    priority        TEXT NOT NULL DEFAULT 'medium',
    assignee_role   TEXT,
    parent_id       INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
    branch          TEXT,
    project         TEXT,
    labels          TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{{}}',
    route           TEXT NOT NULL DEFAULT 'code',
    route_workflow  TEXT,
    route_source    TEXT NOT NULL DEFAULT 'auto',
    route_confidence REAL,
    created_at      TEXT NOT NULL DEFAULT ({_NOW}),
    updated_at      TEXT NOT NULL DEFAULT ({_NOW}),
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS tickets_assignee_status ON tickets(assignee_role, status);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(parent_id);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS tickets_route ON tickets(route, route_workflow);

CREATE TABLE IF NOT EXISTS ticket_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    agent_role  TEXT,
    kind        TEXT NOT NULL,
    body        TEXT,
    metadata    TEXT NOT NULL DEFAULT '{{}}',
    created_at  TEXT NOT NULL DEFAULT ({_NOW})
);
CREATE INDEX IF NOT EXISTS ticket_events_ticket_ts ON ticket_events(ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ticket_events_kind ON ticket_events(kind);

CREATE TABLE IF NOT EXISTS ticket_counter (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    next_n    INTEGER NOT NULL
);
INSERT OR IGNORE INTO ticket_counter (singleton, next_n) VALUES (1, 100);
"""

# Column set inserted by insert_ticket (status omitted -> DB default 'todo').
_INSERT_COLS = (
    "identifier", "title", "body", "priority", "assignee_role", "parent_id",
    "project", "labels", "branch", "metadata", "route", "route_workflow",
    "route_source", "route_confidence",
)


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "labels" in d:
        d["labels"] = json.loads(d.get("labels") or "[]")
    if "metadata" in d:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    for k in ("created_at", "updated_at", "completed_at", "started_at"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d


# Correlated subqueries shared by the enriched list/detail queries.
_STARTED_AT = (
    "(SELECT MIN(created_at) FROM ticket_events "
    " WHERE ticket_id = tickets.id AND kind='status_change' AND body='in_progress')"
    " AS started_at"
)
_ACTIVE_ROLE = (
    "(SELECT agent_role FROM ticket_events "
    " WHERE ticket_id = tickets.id AND agent_role IS NOT NULL "
    " ORDER BY created_at DESC LIMIT 1) AS active_role"
)


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
            # Migrate the route columns onto a PRE-route table BEFORE running the
            # DDL — CREATE TABLE IF NOT EXISTS is a no-op on the existing table,
            # and the DDL's route index would otherwise fail on the missing
            # column. Only ALTER when the table already exists (a fresh DB gets
            # the columns from the DDL). pg_backend ALTERs the same set.
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tickets'"
            ).fetchone()
            if exists:
                have = {r[1] for r in c.execute("PRAGMA table_info(tickets)")}
                for col, ddl in (
                    ("route", "TEXT"), ("route_workflow", "TEXT"),
                    ("route_source", "TEXT"), ("route_confidence", "REAL"),
                ):
                    if col not in have:
                        c.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
                c.commit()
            c.executescript(_DDL)

    def next_counter(self) -> int:
        with _LOCK, self._conn() as c:
            row = c.execute(
                "UPDATE ticket_counter SET next_n = next_n + 1 WHERE singleton = 1 "
                "RETURNING next_n - 1"
            ).fetchone()
        return int(row[0])

    def insert_ticket(self, fields: dict) -> dict:
        values = (
            fields["identifier"], fields["title"], fields.get("body", ""),
            fields.get("priority", "medium"), fields.get("assignee_role"),
            fields.get("parent_id"), fields.get("project"),
            json.dumps(fields.get("labels") or []), fields.get("branch"),
            json.dumps(fields.get("metadata") or {}),
            fields.get("route", "code"), fields.get("route_workflow"),
            fields.get("route_source", "auto"), fields.get("route_confidence"),
        )
        placeholders = ",".join("?" for _ in _INSERT_COLS)
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO tickets ({', '.join(_INSERT_COLS)}) "
                f"VALUES ({placeholders})",
                values,
            )
            r = c.execute("SELECT * FROM tickets WHERE id = ?",
                          (cur.lastrowid,)).fetchone()
        return _row_to_dict(r)

    def fetch_ticket(self, ident_or_id) -> "dict | None":
        with self._conn() as c:
            if isinstance(ident_or_id, int):
                r = c.execute("SELECT * FROM tickets WHERE id = ?",
                              (ident_or_id,)).fetchone()
            else:
                r = c.execute("SELECT * FROM tickets WHERE identifier = ?",
                              (str(ident_or_id),)).fetchone()
        return _row_to_dict(r) if r else None

    def claim_oldest(self, excluded_projects) -> "dict | None":
        sql = "SELECT * FROM tickets WHERE status = 'todo' "
        params: list = []
        if excluded_projects:
            ph = ",".join("?" for _ in excluded_projects)
            sql += f"AND (project IS NULL OR project NOT IN ({ph})) "
            params += list(excluded_projects)
        sql += f"ORDER BY {_PRIORITY_ORDER}, created_at ASC, id ASC LIMIT 1"
        # Atomic claim ACROSS PROCESSES: the module _LOCK only serializes
        # threads in THIS process — a second runner process polling the same
        # SQLite file would otherwise read the same 'todo' row and double-run
        # it. The conditional ``UPDATE ... WHERE status='todo'`` re-checks the
        # status under SQLite's single-writer lock, so only the first claimer
        # flips the row; a loser sees rowcount 0 and retries the next candidate.
        with _LOCK, self._conn() as c:
            # Take the write lock up front so a concurrent runner process
            # can't interleave its read+claim with ours (WAL readers are
            # snapshot-isolated; BEGIN IMMEDIATE serializes the claimers).
            try:
                c.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                pass   # a txn is already open / busy — conditional UPDATE still guards
            for _ in range(50):                  # bounded: skip rows lost to races
                r = c.execute(sql, params).fetchone()
                if r is None:
                    return None
                upd = c.execute(
                    f"UPDATE tickets SET status='in_progress', updated_at={_NOW} "
                    "WHERE id = ? AND status = 'todo'",
                    (r["id"],),
                )
                if upd.rowcount and upd.rowcount > 0:
                    r2 = c.execute("SELECT * FROM tickets WHERE id = ?",
                                   (r["id"],)).fetchone()
                    return _row_to_dict(r2)
                # lost the race for this row — another claimer took it; the
                # next SELECT skips it (no longer 'todo'). Try again.
            return None

    def set_status(self, ticket_id, status, completed, metadata_patch) -> "dict | None":
        with self._conn() as c:
            cur = c.execute("SELECT metadata FROM tickets WHERE id = ?",
                            (ticket_id,)).fetchone()
            if cur is None:
                return None
            merged = json.loads(cur["metadata"] or "{}")
            merged.update(metadata_patch or {})
            sets = [f"status = ?", f"updated_at = {_NOW}", "metadata = ?"]
            params: list = [status, json.dumps(merged)]
            if completed:
                sets.insert(1, f"completed_at = {_NOW}")
            params.append(ticket_id)
            c.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params)
            r = c.execute("SELECT * FROM tickets WHERE id = ?",
                          (ticket_id,)).fetchone()
        return _row_to_dict(r) if r else None

    def delete_ticket(self, ticket_id) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM ticket_events WHERE ticket_id = ?", (ticket_id,))
            cur = c.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            return (cur.rowcount or 0) > 0

    def reset_all_tickets(self) -> int:
        """Delete ALL tickets + events and reset the ONE-<n> counter to its
        seed (100) and the row autoincrement, so the sequence starts over."""
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            c.execute("DELETE FROM ticket_events")
            c.execute("DELETE FROM tickets")
            c.execute("UPDATE ticket_counter SET next_n = 100 WHERE singleton = 1")
            # reset AUTOINCREMENT so row ids restart at 1 too
            c.execute("DELETE FROM sqlite_sequence WHERE name IN "
                      "('tickets', 'ticket_events')")
            return int(n or 0)

    def set_route(self, ident_or_id, route, workflow, source, confidence) -> "dict | None":
        where = "id = ?" if isinstance(ident_or_id, int) else "identifier = ?"
        with self._conn() as c:
            c.execute(
                f"UPDATE tickets SET route=?, route_workflow=?, route_source=?, "
                f"route_confidence=?, updated_at={_NOW} WHERE {where}",
                (route, workflow, source, confidence, ident_or_id),
            )
            r = c.execute(f"SELECT * FROM tickets WHERE {where}",
                          (ident_or_id,)).fetchone()
        return _row_to_dict(r) if r else None

    def set_branch(self, ticket_id, branch) -> None:
        with self._conn() as c:
            c.execute(
                f"UPDATE tickets SET branch = ?, updated_at = {_NOW} WHERE id = ?",
                (branch, ticket_id),
            )

    def append_body(self, ticket_id, extra) -> "dict | None":
        with self._conn() as c:
            c.execute(
                f"UPDATE tickets SET body = body || ?, updated_at = {_NOW} "
                "WHERE id = ?",
                (extra, ticket_id),
            )
            r = c.execute("SELECT * FROM tickets WHERE id = ?",
                          (ticket_id,)).fetchone()
        return _row_to_dict(r) if r else None

    def insert_event(self, ticket_id, agent_role, kind, body, metadata) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ticket_events(ticket_id, agent_role, kind, body, metadata) "
                "VALUES (?,?,?,?,?)",
                (ticket_id, agent_role, kind, body, json.dumps(metadata or {})),
            )
            return int(cur.lastrowid)

    def fetch_events(self, ticket_id, limit) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ticket_id, created_at, agent_role, kind, body, metadata "
                "FROM ticket_events WHERE ticket_id = ? "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_tickets(self, role, statuses, parent_identifier, limit) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if role:
            clauses.append("assignee_role = ?")
            params.append(role)
        if statuses:
            ph = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({ph})")
            params += list(statuses)
        if parent_identifier:
            clauses.append(
                "parent_id = (SELECT id FROM tickets WHERE identifier = ?)")
            params.append(parent_identifier)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            f"SELECT tickets.*, {_STARTED_AT}, {_ACTIVE_ROLE} "
            f"FROM tickets{where} ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_enriched(self, identifier) -> "dict | None":
        with self._conn() as c:
            r = c.execute(
                f"SELECT tickets.*, {_STARTED_AT}, {_ACTIVE_ROLE} "
                "FROM tickets WHERE identifier = ?",
                (identifier,),
            ).fetchone()
        return _row_to_dict(r) if r else None

    def fetch_children(self, parent_id) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tickets WHERE parent_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (parent_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def search_title(self, needle, project, statuses) -> list[dict]:
        ph_status = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM tickets WHERE lower(title) = ? "
        params: list = [needle]
        if project:
            sql += "AND project = ? "
            params.append(project)
        sql += f"AND status IN ({ph_status}) ORDER BY created_at ASC, id ASC LIMIT 20"
        params += list(statuses)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
