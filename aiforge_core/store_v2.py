"""Unified 4-tier memory store backed by Postgres + pgvector + pg_trgm.

Tiers:
  T1 — episodic per-ticket trace, TTL 7 days post-merge
  T2 — semantic cross-ticket facts, human-gated writes
  T3 — procedural recipes, human-gated writes
  T4 — codebase chunks, reindexed from git push
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from . import embed as embed_mod

DEFAULT_DSN = os.environ.get("AIFORGE_PGMEM_DSN",
                              "host=127.0.0.1 port=5432 dbname=aiforge")

VALID_TIERS = {"t1", "t2", "t3", "t4"}

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    tier        TEXT NOT NULL CHECK (tier IN ('t1','t2','t3','t4')),
    wing        TEXT NOT NULL,
    parent_id   TEXT,
    kind        TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    embedding   vector(1024),
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memories_tier_wing  ON memories(tier, wing);
CREATE INDEX IF NOT EXISTS idx_memories_parent     ON memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_expires    ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_embedding  ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_text_trgm  ON memories USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_title_trgm ON memories USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id           BIGSERIAL PRIMARY KEY,
    tier         TEXT NOT NULL CHECK (tier IN ('t2','t3')),
    wing         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_trace TEXT NOT NULL,
    proposed_by  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status);
"""


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


@dataclass
class Memory:
    id: int
    tier: str
    wing: str
    parent_id: str | None
    kind: str
    source: str | None
    title: str | None
    text: str
    metadata: dict
    created_at: datetime
    expires_at: datetime | None


class Store:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def ensure_schema(self) -> None:
        with self._connect() as c, c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            c.commit()

    # ---------- T1 episodic ----------
    def append_event(
        self,
        parent_id: str,
        kind: str,
        text: str,
        *,
        title: str | None = None,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Append an episodic T1 row for a given parent ticket. Returns id."""
        vec = embed_mod.embed(text)
        wing = f"ticket/{parent_id}"
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO memories
                   (tier, wing, parent_id, kind, source, title, text, embedding, metadata)
                   VALUES ('t1', %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)
                   RETURNING id""",
                (wing, parent_id, kind, source, title, text,
                 _vec_literal(vec), json.dumps(metadata or {})),
            )
            rid = cur.fetchone()[0]
            c.commit()
            return rid

    def get_episodic(self, parent_id: str) -> list[Memory]:
        """Return all T1 rows for a parent, chronological."""
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, tier, wing, parent_id, kind, source, title, text,
                          metadata, created_at, expires_at
                   FROM memories
                   WHERE tier = 't1' AND parent_id = %s
                   ORDER BY id ASC""",
                (parent_id,),
            )
            return [
                Memory(
                    id=r[0], tier=r[1], wing=r[2], parent_id=r[3], kind=r[4],
                    source=r[5], title=r[6], text=r[7],
                    metadata=r[8] or {},
                    created_at=r[9], expires_at=r[10],
                )
                for r in cur.fetchall()
            ]

    def mark_ticket_merged(self, parent_id: str, ttl_days: int = 7) -> int:
        """Set expires_at on all T1 rows for this parent."""
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE memories SET expires_at = %s "
                "WHERE tier = 't1' AND parent_id = %s AND expires_at IS NULL",
                (expires, parent_id),
            )
            n = cur.rowcount
            c.commit()
            return n

    def gc_expired(self) -> int:
        """Delete T1 rows past expires_at. Returns deleted count."""
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE tier = 't1' "
                "AND expires_at IS NOT NULL AND expires_at < now()"
            )
            n = cur.rowcount
            c.commit()
            return n
