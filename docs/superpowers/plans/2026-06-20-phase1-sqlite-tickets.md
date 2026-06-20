# Phase 1 — SQLite Tickets Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ticket/state storage work with zero external infra by adding a SQLite backend behind the existing `aiforge_core.tickets.store` API, defaulting to SQLite and using Postgres only when `AIFORGE_PG_URL` is set.

**Architecture:** Extract the current psycopg logic into a `PgBackend` class implementing a `StoreBackend` protocol. Add a `SqliteBackend` with the same surface. `store.py` keeps its module-level public functions (`create`, `get`, `claim_next_any`, …) as thin delegators to a process-wide backend chosen once by a factory. Callers (`api.py`, runtime) are untouched.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, existing `psycopg` (Postgres path), `pytest`.

This is the foundation phase of the deploy-anywhere effort (spec: `docs/superpowers/specs/2026-06-20-deploy-anywhere-design.md`). It unblocks `./run.sh` booting with no Postgres. Memory, providers, home page, chat, and `run.sh` are separate phases/plans.

---

## File Structure

- **Create** `aiforge_core/tickets/backends/__init__.py` — empty package marker.
- **Create** `aiforge_core/tickets/backends/base.py` — `StoreBackend` Protocol + shared `Ticket`-row helpers and SQL-agnostic constants.
- **Create** `aiforge_core/tickets/backends/sqlite_backend.py` — `SqliteBackend`, SQLite DDL + CRUD.
- **Create** `aiforge_core/tickets/backends/pg_backend.py` — `PgBackend`, the extracted current psycopg logic.
- **Create** `aiforge_core/tickets/backend_factory.py` — `get_backend()` selector (env-driven, memoized).
- **Modify** `aiforge_core/tickets/store.py` — keep public functions, delegate to `backend_factory.get_backend()`. Keep `Ticket`, `VALID_STATUS`, `VALID_PRIORITY` where they are.
- **Modify** `aiforge_core/config/env.py` — add `AIFORGE_PG_URL` + `AIFORGE_DB_PATH` resolution; `AIFORGE_USE_SQLITE` derived flag.
- **Test** `tests/tickets/test_sqlite_backend.py` — SQLite backend contract tests.
- **Test** `tests/tickets/test_backend_factory.py` — selector tests.
- **Test** `tests/tickets/test_store_delegates.py` — store public API works end-to-end on SQLite.

We keep `Ticket`, `VALID_STATUS`, `VALID_PRIORITY` in `store.py` to avoid churn in importers that do `from aiforge_core.tickets.store import Ticket`.

---

## Task 1: Backend protocol + env flags

**Files:**
- Create: `aiforge_core/tickets/backends/__init__.py`
- Create: `aiforge_core/tickets/backends/base.py`
- Modify: `aiforge_core/config/env.py`
- Test: `tests/tickets/test_backend_factory.py` (env part only this task)

- [ ] **Step 1: Add env resolution**

In `aiforge_core/config/env.py`, after the existing `AIFORGE_DSN` block, add:

```python
# ─────────────────────────── storage backend ───────────────────────────
# Default to embedded SQLite. Postgres only when AIFORGE_PG_URL is set
# (or the legacy AIFORGE_DSN explicitly points at a postgres:// URL).
AIFORGE_PG_URL = os.environ.get("AIFORGE_PG_URL") or (
    AIFORGE_DSN if str(AIFORGE_DSN).startswith(("postgres://", "postgresql://"))
    and os.environ.get("AIFORGE_FORCE_PG") == "1"
    else None
)
AIFORGE_DB_PATH = os.environ.get(
    "AIFORGE_DB_PATH",
    os.path.join(os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
                 "aiforge.db"),
)
AIFORGE_USE_SQLITE = AIFORGE_PG_URL is None
```

Note: `AIFORGE_DSN` keeps its current default so nothing else breaks, but it no longer auto-selects Postgres for the ticket store — only an explicit `AIFORGE_PG_URL` (or `AIFORGE_FORCE_PG=1`) does.

- [ ] **Step 2: Write the failing env test**

Create `tests/tickets/test_backend_factory.py`:

```python
import importlib
import os

import pytest


def _reload_env(monkeypatch, **env):
    for k in ("AIFORGE_PG_URL", "AIFORGE_DB_PATH", "AIFORGE_FORCE_PG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import aiforge_core.config.env as envmod
    return importlib.reload(envmod)


def test_default_is_sqlite(monkeypatch):
    envmod = _reload_env(monkeypatch)
    assert envmod.AIFORGE_USE_SQLITE is True
    assert envmod.AIFORGE_PG_URL is None


def test_pg_url_selects_postgres(monkeypatch):
    envmod = _reload_env(monkeypatch, AIFORGE_PG_URL="postgresql://x/y")
    assert envmod.AIFORGE_USE_SQLITE is False
    assert envmod.AIFORGE_PG_URL == "postgresql://x/y"
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/tickets/test_backend_factory.py -v`
Expected: both PASS (env code from Step 1 already present).

- [ ] **Step 4: Write the backend protocol**

Create `aiforge_core/tickets/backends/__init__.py` (empty file).

Create `aiforge_core/tickets/backends/base.py`:

```python
"""Storage backend protocol for the ticket store.

Both PgBackend and SqliteBackend implement this surface. store.py
delegates its module-level functions to whichever the factory picks.
All methods return plain dict rows (psycopg dict_row shape); store.py
wraps them into Ticket via Ticket.from_row.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StoreBackend(Protocol):
    def ensure_schema(self) -> None: ...

    def new_identifier(self) -> str: ...

    def create(self, fields: dict) -> dict: ...

    def get(self, ident_or_id: "str | int") -> "dict | None": ...

    def claim_next_any(self, aliases: list[str], excluded_projects: list[str]) -> "dict | None": ...

    def update_status(self, ticket_id: int, status: str, role: "str | None",
                      extra: dict) -> "dict | None": ...

    def update_route(self, ticket_id: int, route: str, workflow: "str | None",
                     source: str, confidence: "float | None") -> "dict | None": ...

    def add_comment(self, ticket_id: int, role: "str | None", body: str) -> int: ...

    def add_event(self, ticket_id: int, role: "str | None", kind: str,
                  body: "str | None", metadata: "dict | None") -> int: ...

    def children(self, parent_id: int) -> list[dict]: ...

    def by_title_project(self, title: str, project: "str | None") -> list[dict]: ...

    def comments(self, ticket_id: int, limit: int) -> list[dict]: ...
```

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/config/env.py aiforge_core/tickets/backends/__init__.py \
        aiforge_core/tickets/backends/base.py tests/tickets/test_backend_factory.py
git commit -m "feat(tickets): storage backend protocol + sqlite-default env flags"
```

---

## Task 2: SqliteBackend — schema + new_identifier + create + get

**Files:**
- Create: `aiforge_core/tickets/backends/sqlite_backend.py`
- Test: `tests/tickets/test_sqlite_backend.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tickets/test_sqlite_backend.py`:

```python
import pytest

from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend


@pytest.fixture
def be(tmp_path):
    b = SqliteBackend(str(tmp_path / "t.db"))
    b.ensure_schema()
    return b


def test_new_identifier_increments(be):
    a = be.new_identifier()
    b = be.new_identifier()
    assert a == "ONE-1"
    assert b == "ONE-2"


def test_create_and_get(be):
    ident = be.new_identifier()
    row = be.create({
        "identifier": ident, "title": "hello", "body": "world",
        "status": "todo", "priority": "medium", "assignee_role": "doer",
        "parent_id": None, "branch": None, "project": "demo",
        "labels": ["x", "y"], "metadata": {"k": 1},
        "route": "code", "route_workflow": None,
        "route_source": "auto", "route_confidence": None,
    })
    assert row["identifier"] == ident
    assert row["labels"] == ["x", "y"]
    assert row["metadata"] == {"k": 1}
    got = be.get(ident)
    assert got["title"] == "hello"
    got_by_id = be.get(row["id"])
    assert got_by_id["id"] == row["id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tickets/test_sqlite_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: ...sqlite_backend`.

- [ ] **Step 3: Write the SQLite backend (schema + identifier + create + get)**

Create `aiforge_core/tickets/backends/sqlite_backend.py`:

```python
"""SQLite implementation of StoreBackend — zero-infra default.

JSON columns (labels, metadata) are stored as TEXT and (de)serialized
here. Timestamps are ISO-8601 TEXT via CURRENT_TIMESTAMP. Identifiers
come from a single-row counter table updated atomically.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
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

_COLS = (
    "id", "identifier", "title", "body", "status", "priority", "assignee_role",
    "parent_id", "branch", "project", "labels", "metadata", "route",
    "route_workflow", "route_source", "route_confidence",
    "created_at", "updated_at", "completed_at",
)


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/tickets/test_sqlite_backend.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/tickets/backends/sqlite_backend.py tests/tickets/test_sqlite_backend.py
git commit -m "feat(tickets): SqliteBackend schema + new_identifier + create + get"
```

---

## Task 3: SqliteBackend — claim_next_any + update_status + update_route

**Files:**
- Modify: `aiforge_core/tickets/backends/sqlite_backend.py`
- Test: `tests/tickets/test_sqlite_backend.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/tickets/test_sqlite_backend.py`:

```python
def _mk(be, role, status="todo", project="demo"):
    ident = be.new_identifier()
    return be.create({
        "identifier": ident, "title": "t", "body": "", "status": status,
        "priority": "medium", "assignee_role": role, "parent_id": None,
        "branch": None, "project": project, "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })


def test_claim_next_any_oldest_first(be):
    first = _mk(be, "doer")
    _mk(be, "doer")
    claimed = be.claim_next_any(aliases=["doer"], excluded_projects=[])
    assert claimed["id"] == first["id"]


def test_claim_excludes_projects(be):
    _mk(be, "doer", project="skipme")
    keep = _mk(be, "doer", project="demo")
    claimed = be.claim_next_any(aliases=["doer"], excluded_projects=["skipme"])
    assert claimed["id"] == keep["id"]


def test_update_status_sets_completed(be):
    t = _mk(be, "doer")
    out = be.update_status(t["id"], "done", role="doer", extra={})
    assert out["status"] == "done"
    assert out["completed_at"] is not None


def test_update_route(be):
    t = _mk(be, "doer")
    out = be.update_route(t["id"], "workflow", "wf-1", "manual", 0.9)
    assert out["route"] == "workflow"
    assert out["route_workflow"] == "wf-1"
    assert out["route_confidence"] == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tickets/test_sqlite_backend.py -k "claim or update" -v`
Expected: FAIL with `AttributeError: 'SqliteBackend' object has no attribute 'claim_next_any'`.

- [ ] **Step 3: Implement the three methods**

Append to the `SqliteBackend` class in `aiforge_core/tickets/backends/sqlite_backend.py`:

```python
    def claim_next_any(self, aliases, excluded_projects) -> "dict | None":
        if not aliases:
            return None
        ph_roles = ",".join("?" for _ in aliases)
        sql = (
            f"SELECT * FROM tickets "
            f"WHERE status = 'todo' AND assignee_role IN ({ph_roles}) "
        )
        params = list(aliases)
        if excluded_projects:
            ph_proj = ",".join("?" for _ in excluded_projects)
            sql += f"AND (project IS NULL OR project NOT IN ({ph_proj})) "
            params += list(excluded_projects)
        sql += "ORDER BY created_at ASC, id ASC LIMIT 1"
        with self._conn() as c:
            r = c.execute(sql, params).fetchone()
            if not r:
                return None
            c.execute(
                "UPDATE tickets SET status='in_progress', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (r["id"],),
            )
            r2 = c.execute("SELECT * FROM tickets WHERE id=?", (r["id"],)).fetchone()
        return _row_to_dict(r2)

    def update_status(self, ticket_id, status, role, extra) -> "dict | None":
        sets = ["status = ?", "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
        params = [status]
        if status == "done":
            sets.append("completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        for k in ("branch", "assignee_role", "parent_id"):
            if k in extra:
                sets.append(f"{k} = ?")
                params.append(extra[k])
        params.append(ticket_id)
        with self._conn() as c:
            c.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params)
            r = c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _row_to_dict(r) if r else None

    def update_route(self, ticket_id, route, workflow, source, confidence) -> "dict | None":
        with self._conn() as c:
            c.execute(
                "UPDATE tickets SET route=?, route_workflow=?, route_source=?, "
                "route_confidence=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=?",
                (route, workflow, source, confidence, ticket_id),
            )
            r = c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _row_to_dict(r) if r else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/tickets/test_sqlite_backend.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/tickets/backends/sqlite_backend.py tests/tickets/test_sqlite_backend.py
git commit -m "feat(tickets): SqliteBackend claim/update_status/update_route"
```

---

## Task 4: SqliteBackend — events, comments, children, by_title_project

**Files:**
- Modify: `aiforge_core/tickets/backends/sqlite_backend.py`
- Test: `tests/tickets/test_sqlite_backend.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/tickets/test_sqlite_backend.py`:

```python
def test_events_and_comments(be):
    t = _mk(be, "doer")
    eid = be.add_event(t["id"], "doer", "note", "did a thing", {"a": 1})
    assert eid > 0
    cid = be.add_comment(t["id"], "doer", "a comment")
    assert cid > 0
    cs = be.comments(t["id"], limit=10)
    assert any(c["body"] == "a comment" for c in cs)


def test_children(be):
    parent = _mk(be, "planner")
    ident = be.new_identifier()
    child = be.create({
        "identifier": ident, "title": "c", "body": "", "status": "todo",
        "priority": "medium", "assignee_role": "doer", "parent_id": parent["id"],
        "branch": None, "project": "demo", "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })
    kids = be.children(parent["id"])
    assert [k["id"] for k in kids] == [child["id"]]


def test_by_title_project(be):
    _mk(be, "doer")
    ident = be.new_identifier()
    be.create({
        "identifier": ident, "title": "unique-title", "body": "", "status": "todo",
        "priority": "medium", "assignee_role": "doer", "parent_id": None,
        "branch": None, "project": "demo", "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })
    hits = be.by_title_project("unique-title", "demo")
    assert len(hits) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tickets/test_sqlite_backend.py -k "events or children or title" -v`
Expected: FAIL with `AttributeError: ...add_event`.

- [ ] **Step 3: Implement the methods**

Append to `SqliteBackend`:

```python
    def add_event(self, ticket_id, role, kind, body, metadata) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ticket_events(ticket_id, role, kind, body, metadata) "
                "VALUES (?,?,?,?,?)",
                (ticket_id, role, kind, body, json.dumps(metadata or {})),
            )
            return int(cur.lastrowid)

    def add_comment(self, ticket_id, role, body) -> int:
        return self.add_event(ticket_id, role, "comment", body, {})

    def comments(self, ticket_id, limit) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, role, kind, body, metadata, created_at FROM ticket_events "
                "WHERE ticket_id = ? AND kind = 'comment' "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d.get("metadata") or "{}")
            out.append(d)
        return out

    def children(self, parent_id) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tickets WHERE parent_id = ? ORDER BY created_at ASC, id ASC",
                (parent_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def by_title_project(self, title, project) -> list[dict]:
        with self._conn() as c:
            if project is None:
                rows = c.execute(
                    "SELECT * FROM tickets WHERE title = ? AND project IS NULL",
                    (title,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM tickets WHERE title = ? AND project = ?",
                    (title, project),
                ).fetchall()
        return [_row_to_dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/tickets/test_sqlite_backend.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/tickets/backends/sqlite_backend.py tests/tickets/test_sqlite_backend.py
git commit -m "feat(tickets): SqliteBackend events/comments/children/by_title_project"
```

---

## Task 5: PgBackend — wrap the existing psycopg logic

**Files:**
- Create: `aiforge_core/tickets/backends/pg_backend.py`
- Reference (do not delete yet): `aiforge_core/tickets/store.py:145-481`

This task moves the current SQL into a class with the same method names as `SqliteBackend`, so the Postgres path is preserved verbatim. No behavior change.

- [ ] **Step 1: Write the failing import test**

Append to `tests/tickets/test_backend_factory.py`:

```python
def test_pg_backend_importable():
    from aiforge_core.tickets.backends.pg_backend import PgBackend
    assert hasattr(PgBackend, "claim_next_any")
    assert hasattr(PgBackend, "create")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tickets/test_backend_factory.py::test_pg_backend_importable -v`
Expected: FAIL with `ModuleNotFoundError: ...pg_backend`.

- [ ] **Step 3: Create PgBackend**

Create `aiforge_core/tickets/backends/pg_backend.py`. Move the bodies of the current
`store.py` helpers (`_ensure_schema`, `_conn`, `new_identifier`, `create`, `get`,
`claim_next_any`, `update_status`, `update_route`, `add_comment`, `add_event`, `children`,
`by_title_project`, `comments`) into methods on this class, changing `self`-free module
functions into methods. Method signatures must match `base.StoreBackend`. Use this skeleton and
port each body **exactly** from `store.py` (return raw dict rows from `dict_row` cursors; do NOT
wrap in `Ticket`):

```python
"""Postgres implementation of StoreBackend — the original store logic.

Ported verbatim from store.py. Used only when AIFORGE_PG_URL is set.
Returns raw dict rows; store.py wraps them in Ticket.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from aiforge_core.config.env import AIFORGE_PG_URL
from aiforge_core.tickets.store import _DDL_SQL  # see Task 6 note below


class PgBackend:
    name = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as c:
            yield c

    def ensure_schema(self) -> None:
        with self._conn() as c:
            c.execute(_DDL_SQL)
            c.commit()

    # ... port new_identifier/create/get/claim_next_any/update_status/
    #     update_route/add_comment/add_event/children/by_title_project/
    #     comments here, each body copied from store.py, `self`-bound,
    #     returning dict rows (the dicts dict_row already yields).
```

Note for the porter: the current `store.py` interleaves `Ticket.from_row(...)` wrapping inside
its functions. In `PgBackend`, return the raw `dict` row instead (drop the `Ticket.from_row`
call); `store.py` (Task 6) does the wrapping. For `create`, accept the same `fields` dict shape
that `SqliteBackend.create` accepts and build the INSERT from it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/tickets/test_backend_factory.py::test_pg_backend_importable -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/tickets/backends/pg_backend.py tests/tickets/test_backend_factory.py
git commit -m "feat(tickets): PgBackend wrapping original psycopg logic"
```

---

## Task 6: Factory + store.py delegation

**Files:**
- Create: `aiforge_core/tickets/backend_factory.py`
- Modify: `aiforge_core/tickets/store.py`
- Test: `tests/tickets/test_store_delegates.py`

- [ ] **Step 1: Add the factory**

Create `aiforge_core/tickets/backend_factory.py`:

```python
"""Pick the ticket storage backend once per process.

SQLite by default (AIFORGE_DB_PATH); Postgres when AIFORGE_PG_URL set.
"""
from __future__ import annotations

import threading

from aiforge_core.config.env import AIFORGE_PG_URL, AIFORGE_DB_PATH, AIFORGE_USE_SQLITE

_LOCK = threading.Lock()
_BACKEND = None


def get_backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is None:
            if AIFORGE_USE_SQLITE:
                from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
                be = SqliteBackend(AIFORGE_DB_PATH)
            else:
                from aiforge_core.tickets.backends.pg_backend import PgBackend
                be = PgBackend(AIFORGE_PG_URL)
            be.ensure_schema()
            _BACKEND = be
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None
```

- [ ] **Step 2: Refactor store.py to delegate**

In `aiforge_core/tickets/store.py`:

1. Keep `Ticket`, `VALID_STATUS`, `VALID_PRIORITY`, `_apply_supervisor_invariants`, `_aliases_for`, `_excluded_projects`.
2. Move the DDL string the schema bootstrap uses into a module constant named `_DDL_SQL` (PgBackend imports it).
3. Replace each public function body with a delegation that calls `get_backend()` and wraps dict rows in `Ticket`. Keep the public signatures identical so callers don't change.

Example of the delegation shape (apply the same pattern to every public function):

```python
from aiforge_core.tickets.backend_factory import get_backend


def new_identifier() -> str:
    return get_backend().new_identifier()


def create(title: str, body: str = "", *, status: str = "todo",
           priority: str = "medium", assignee_role: str | None = None,
           parent_id: int | None = None, branch: str | None = None,
           project: str | None = None, labels: list[str] | None = None,
           metadata: dict | None = None, route: str = "code",
           route_workflow: str | None = None, route_source: str = "auto",
           route_confidence: float | None = None) -> Ticket:
    ident = new_identifier()
    fields = {
        "identifier": ident, "title": title, "body": body, "status": status,
        "priority": priority, "assignee_role": assignee_role, "parent_id": parent_id,
        "branch": branch, "project": project, "labels": labels or [],
        "metadata": metadata or {}, "route": route, "route_workflow": route_workflow,
        "route_source": route_source, "route_confidence": route_confidence,
    }
    _apply_supervisor_invariants(fields)  # keep existing invariant logic, adapted to dict
    return Ticket.from_row(get_backend().create(fields))


def get(ident_or_id: str | int) -> Ticket | None:
    row = get_backend().get(ident_or_id)
    return Ticket.from_row(row) if row else None


def claim_next_any() -> Ticket | None:
    row = get_backend().claim_next_any(_aliases_for("doer"), _excluded_projects())
    return Ticket.from_row(row) if row else None


def update_status(ticket_id: int, status: str, *, role: str | None = None, **extra) -> Ticket | None:
    if status not in VALID_STATUS:
        raise ValueError(f"bad status {status}")
    row = get_backend().update_status(ticket_id, status, role, extra)
    return Ticket.from_row(row) if row else None
```

Port the remaining public functions (`update_route`, `add_comment`, `add_event`, `children`,
`by_title_project`, `comments`) to the same delegation pattern. Adapt `_apply_supervisor_invariants`
to mutate the `fields` dict (it currently operates on call args).

Remove the now-unused `psycopg` import and `_conn`/`_ensure_schema` from `store.py` (they live in
`pg_backend.py` now). Keep `_DDL_SQL` exported.

- [ ] **Step 3: Write the delegation test**

Create `tests/tickets/test_store_delegates.py`:

```python
import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "s.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    return store


def test_store_create_get_roundtrip(store):
    t = store.create("hello", "body", project="demo", assignee_role="doer", labels=["a"])
    assert t.identifier.startswith("ONE-")
    got = store.get(t.identifier)
    assert got.title == "hello"
    assert got.labels == ["a"]


def test_store_claim_and_status(store):
    t = store.create("work", assignee_role="doer", project="demo")
    claimed = store.claim_next_any()
    assert claimed.id == t.id
    done = store.update_status(t.id, "done")
    assert done.status == "done"
    assert done.completed_at is not None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/tickets/test_store_delegates.py -v`
Expected: both PASS.

- [ ] **Step 5: Run the full ticket test suite**

Run: `pytest tests/tickets/ -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/tickets/store.py aiforge_core/tickets/backend_factory.py \
        aiforge_core/tickets/backends/pg_backend.py tests/tickets/test_store_delegates.py
git commit -m "feat(tickets): factory + store delegation (sqlite default, pg opt-in)"
```

---

## Task 7: API boot smoke — no Postgres required

**Files:**
- Modify: `aiforge_core/api/api.py` (only if it imports `psycopg` at module top for ticket reads)
- Test: `tests/api/test_boot_no_pg.py`

- [ ] **Step 1: Audit api.py for direct psycopg ticket access**

Run: `grep -n "psycopg\|AIFORGE_DSN\|dict_row" aiforge_core/api/api.py`
Expected: identify any ticket/state reads that bypass `store`. (Memory routes that hit Postgres
directly are out of scope — Phase 2 handles memory; if a memory route imports psycopg at module
top and would crash boot, wrap that import lazily inside the route function.)

- [ ] **Step 2: Write the failing boot test**

Create `tests/api/test_boot_no_pg.py`:

```python
import importlib

from fastapi.testclient import TestClient


def test_app_boots_and_lists_tickets_on_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    store.create("seed", assignee_role="doer", project="demo")

    import aiforge_core.api.api as api
    importlib.reload(api)
    client = TestClient(api.app)

    assert client.get("/api/health").status_code == 200
    r = client.get("/api/tickets")
    assert r.status_code == 200
    assert any(t["title"] == "seed" for t in r.json())
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/api/test_boot_no_pg.py -v`
Expected: PASS. If it fails because a module-top `psycopg.connect(...)` for memory runs at
import, make that import/connection lazy (move it inside the handler). Do NOT convert memory to
SQLite here — that is Phase 2. Only stop module import from requiring a live Postgres.

- [ ] **Step 4: Run the whole suite**

Run: `pytest -q`
Expected: green (pre-existing unrelated failures, if any, noted but not introduced by this phase).

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/api/api.py tests/api/test_boot_no_pg.py
git commit -m "feat(api): boot + list tickets on SQLite with no Postgres"
```

---

## Self-Review Notes (resolved)

- **Spec coverage:** Implements spec §4.1 (tickets SQLite backend) and confirmation **A**. Memory (§4.2), providers (§5), home/chat (§6), permissions/deploy (§7) are explicitly later phases — not in this plan.
- **Type consistency:** Backend methods return raw `dict` rows everywhere; only `store.py` wraps in `Ticket`. `create` takes a `fields` dict in both backends. `_DDL_SQL` is the single shared DDL name imported by `PgBackend`.
- **No auto-migration:** matches spec §4.3 / §10. Existing Postgres users set `AIFORGE_PG_URL` and keep their data.
- **Risk:** `_apply_supervisor_invariants` currently mutates call args; Task 6 adapts it to mutate the `fields` dict — the porter must verify the invariant fields (assignee defaults, label normalization) still apply. Flagged in Task 6 Step 2.

---

## Next phases (separate plans, written after this lands)

2. SQLite + vector memory backend + embed fallback (spec §4.2)
3. `openai_compatible` provider + config-driven base_url/key/model (spec §5)
4. Home page config landing + provider test-connection (spec §6.1)
5. Chat SSE endpoint + full-FS ReAct loop + Chat.tsx wiring (spec §6.2)
6. `run.sh` one-command boot + README + permission warning (spec §7)
