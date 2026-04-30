"""codemem state database (sqlite).

Tables:
    merkle_repo       (repo TEXT PK, pack_sha TEXT, last_packed REAL)
    merkle_files      (repo TEXT, path TEXT, hash TEXT, last_indexed REAL,
                       PRIMARY KEY (repo, path))
    service_overrides (repo TEXT, name TEXT, source TEXT, payload TEXT,
                       PRIMARY KEY (repo, name))
    query_cache       (key TEXT PK, bundle_json TEXT, expires_at REAL)
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "AIFORGE_CODEMEM_STATE_DB",
        os.path.expanduser("~/.aiforge/codemem.state.db"),
    )
)


def open_db(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_DDL = [
    """CREATE TABLE IF NOT EXISTS merkle_repo (
        repo        TEXT PRIMARY KEY,
        pack_sha    TEXT NOT NULL,
        last_packed REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS merkle_files (
        repo         TEXT NOT NULL,
        path         TEXT NOT NULL,
        hash         TEXT NOT NULL,
        last_indexed REAL NOT NULL,
        PRIMARY KEY (repo, path)
    )""",
    """CREATE TABLE IF NOT EXISTS service_overrides (
        repo    TEXT NOT NULL,
        name    TEXT NOT NULL,
        source  TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (repo, name)
    )""",
    """CREATE TABLE IF NOT EXISTS query_cache (
        key         TEXT PRIMARY KEY,
        bundle_json TEXT NOT NULL,
        expires_at  REAL NOT NULL
    )""",
]


def migrate(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for stmt in _DDL:
        cur.execute(stmt)
    conn.commit()


def set_repo_pack_sha(conn: sqlite3.Connection, *, repo: str, pack_sha: str) -> None:
    conn.execute(
        "INSERT INTO merkle_repo (repo, pack_sha, last_packed) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(repo) DO UPDATE SET pack_sha=excluded.pack_sha, "
        "  last_packed=excluded.last_packed",
        (repo, pack_sha, time.time()),
    )
    conn.commit()


def get_repo_pack_sha(conn: sqlite3.Connection, *, repo: str) -> str | None:
    row = conn.execute(
        "SELECT pack_sha FROM merkle_repo WHERE repo = ?", (repo,)
    ).fetchone()
    return row[0] if row else None
