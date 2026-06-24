"""Embedded SQLite memory store — zero-infra observation/recall.

Stores memory units (the agent's learnings, failures, self-writes) in
``~/.aiforge/memory.db`` with an offline hash embedding, and recalls by
brute-force cosine. Used when ``backend_select.embedded()`` is True
(no Neo4j / Postgres configured). Quality is lexical, not semantic —
the "pro" Neo4j/bge-m3 path supersedes it when its env vars are set.

Public surface:
    write_unit(*, text, kind, ...) -> int        # 0 when skipped (dup/empty)
    recall(text, *, limit, repo) -> list[dict]
    stats() -> dict
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from aiforge_core.memory import local_embed

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS memory_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'observation',
    wing        TEXT,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}',
    repo        TEXT,
    ticket      TEXT,
    embedding   TEXT NOT NULL DEFAULT '[]',
    event_time  REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS memory_units_repo ON memory_units(repo);
CREATE INDEX IF NOT EXISTS memory_units_kind ON memory_units(kind);
CREATE INDEX IF NOT EXISTS memory_units_ticket ON memory_units(ticket);
"""


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_MEMORY_DB_PATH",
        os.path.join(
            os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
            "memory.db",
        ),
    )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.executescript(_DDL)
        yield c
        c.commit()
    finally:
        c.close()


def write_unit(
    *,
    text: str,
    kind: str = "observation",
    wing: str | None = None,
    source: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    repo: str | None = None,
    ticket: str | None = None,
    event_time: float | None = None,
) -> int:
    """Insert a memory unit, returning its row id (0 if skipped).

    Skips empty text and exact ``(repo, text)`` duplicates so repeated
    runs don't bloat the store — mirrors the AFM pure-text dedupe.
    """
    text = (text or "").strip()
    if not text:
        return 0
    vec = local_embed.embed(text)
    with _LOCK, _conn() as c:
        dup = c.execute(
            "SELECT id FROM memory_units WHERE text = ? AND "
            "(repo IS ? OR repo = ?) LIMIT 1",
            (text, repo, repo),
        ).fetchone()
        if dup:
            return 0
        cur = c.execute(
            "INSERT INTO memory_units "
            "(kind, wing, source, title, text, tags, metadata, repo, ticket, "
            " embedding, event_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind, wing, source, title, text,
                json.dumps(tags or []), json.dumps(metadata or {}),
                repo, ticket, json.dumps(vec), event_time,
            ),
        )
        return int(cur.lastrowid)


def recall(text: str, *, limit: int = 8, repo: str | None = None) -> list[dict]:
    """Brute-force cosine recall. Returns hits sorted by score desc.

    Each hit: ``{text, title, source, kind, ticket, repo, score}`` with
    ``score`` the clamped cosine in [0, 1]. ``repo`` filters to that
    repo plus repo-agnostic rows when provided.
    """
    text = (text or "").strip()
    if not text:
        return []
    qvec = local_embed.embed(text)
    if not any(qvec):
        return []
    with _conn() as c:
        if repo:
            rows = c.execute(
                "SELECT * FROM memory_units WHERE repo = ? OR repo IS NULL",
                (repo,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM memory_units").fetchall()
    scored: list[dict] = []
    for r in rows:
        try:
            vec = json.loads(r["embedding"] or "[]")
        except (TypeError, ValueError):
            continue
        score = local_embed.cosine(qvec, vec)
        if score <= 0.0:
            continue
        scored.append({
            "text": r["text"],
            "title": r["title"],
            "source": r["source"] or "memory",
            "kind": r["kind"],
            "ticket": r["ticket"],
            "repo": r["repo"],
            "score": max(0.0, min(1.0, score)),
        })
    scored.sort(key=lambda h: -h["score"])
    # Dedup by text (keep the highest score) — the same learning can exist
    # both repo-scoped and repo-agnostic (write_unit dedup is per-(repo,text)),
    # and recall unions repo + NULL rows, so identical text would surface twice.
    seen: set[str] = set()
    out: list[dict] = []
    for h in scored:
        key = h["text"]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def stats() -> dict:
    """Counts for health / dashboard. Soft — returns zeros on error."""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
            by_kind = {
                row["kind"]: row["n"]
                for row in c.execute(
                    "SELECT kind, COUNT(*) AS n FROM memory_units GROUP BY kind"
                ).fetchall()
            }
        return {"backend": "sqlite", "total": int(total), "by_kind": by_kind,
                "db_path": _db_path()}
    except Exception:
        return {"backend": "sqlite", "total": 0, "by_kind": {},
                "db_path": _db_path()}
