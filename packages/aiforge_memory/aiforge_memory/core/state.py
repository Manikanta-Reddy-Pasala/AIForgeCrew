"""codemem state database (sqlite).

Tables:
    merkle_repo       (repo TEXT PK, pack_sha TEXT, last_packed REAL)
    merkle_files      (repo TEXT, path TEXT, hash TEXT, last_indexed REAL,
                       PRIMARY KEY (repo, path))
    service_overrides (repo TEXT, name TEXT, source TEXT, payload TEXT,
                       PRIMARY KEY (repo, name))
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
    """Open the state DB. Allows cross-thread use because the scheduler
    daemon's per-tick worker thread (used to enforce the timeout) needs
    to call into state_db while the connection was opened on the main
    thread. Ticks run serially in the loop, so there is at most one
    writer at any instant — no extra locking required."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
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
    """CREATE TABLE IF NOT EXISTS git_state (
        repo        TEXT PRIMARY KEY,
        head_sha    TEXT NOT NULL,
        branch      TEXT NOT NULL,
        last_seen   REAL NOT NULL
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


def get_repo_git_head(
    conn: sqlite3.Connection, *, repo: str,
) -> tuple[str, str] | None:
    """Return (head_sha, branch) for repo, or None."""
    row = conn.execute(
        "SELECT head_sha, branch FROM git_state WHERE repo = ?", (repo,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def set_repo_git_head(
    conn: sqlite3.Connection, *, repo: str, head_sha: str, branch: str,
) -> None:
    conn.execute(
        "INSERT INTO git_state (repo, head_sha, branch, last_seen) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo) DO UPDATE SET head_sha=excluded.head_sha, "
        "  branch=excluded.branch, last_seen=excluded.last_seen",
        (repo, head_sha, branch, time.time()),
    )
    conn.commit()


def get_file_hashes(conn: sqlite3.Connection, *, repo: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT path, hash FROM merkle_files WHERE repo = ?", (repo,)
    ).fetchall()
    return {p: h for p, h in rows}


def upsert_file_hash(
    conn: sqlite3.Connection, *, repo: str, path: str, file_hash: str,
) -> None:
    conn.execute(
        "INSERT INTO merkle_files (repo, path, hash, last_indexed) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo, path) DO UPDATE SET hash=excluded.hash, "
        "  last_indexed=excluded.last_indexed",
        (repo, path, file_hash, time.time()),
    )
    conn.commit()


def upsert_file_hashes(
    conn: sqlite3.Connection, *, repo: str, hashes: dict[str, str],
) -> None:
    """Bulk upsert per-file hashes — used by delta ingest."""
    if not hashes:
        return
    now = time.time()
    conn.executemany(
        "INSERT INTO merkle_files (repo, path, hash, last_indexed) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo, path) DO UPDATE SET hash=excluded.hash, "
        "  last_indexed=excluded.last_indexed",
        [(repo, p, h, now) for p, h in hashes.items()],
    )
    conn.commit()
