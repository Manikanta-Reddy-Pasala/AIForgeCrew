"""PgVector-backed two-tier memory (replaces the MemPalace shell-out).

Same two-tier ACL as aiforge_core.mem.MemBus:
  - `project` scope (wing='project'): writers = em + sr-architect only.
  - `own`     scope (wing='agent/<role>'): writer = owner only; readers = all.

Embeddings via LM Studio's nomic-embed-text endpoint
(http://localhost:1234/v1/embeddings), 768-dim, fully local.

Schema created by scripts/install-pgvector-macstudio.sh:
  memories(id, wing, room, source, title, text, embedding vector(768), metadata, created_at)

Install: `uv pip install psycopg[binary]` (already in [mem] extras).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from .permissions import PermissionDenied

PROJECT_WRITERS = {"em", "sr-architect"}
ALL_ROLES = {"em", "tester", "sr-developer", "sr-architect"}

DEFAULT_DSN = os.environ.get("AIFORGE_PGMEM_DSN", "host=127.0.0.1 port=5432 dbname=aiforge")
DEFAULT_EMBED_URL = os.environ.get("LLM_ENDPOINT", "http://localhost:1234/v1") + "/embeddings"
DEFAULT_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"


# -------- embedding --------

def embed(text: str, *, model: str = DEFAULT_EMBED_MODEL,
          url: str = DEFAULT_EMBED_URL, timeout: float = 30.0) -> list[float]:
    body = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return resp["data"][0]["embedding"]


def _vec_literal(v: list[float]) -> str:
    """Render a Python float list as a pgvector-compatible string literal."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# -------- bus --------

@dataclass
class PgMemBus:
    """Two-tier memory bus backed by pgvector on localhost Postgres."""

    dsn: str = DEFAULT_DSN
    embed_model: str = DEFAULT_EMBED_MODEL

    def _connect(self):
        import psycopg  # lazy import — only needed when the bus is used
        return psycopg.connect(self.dsn, autocommit=False)

    def ensure_schema(self) -> None:
        """Create `memories` table + HNSW index if missing. Idempotent."""
        with self._connect() as c, c.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          BIGSERIAL PRIMARY KEY,
                    wing        TEXT NOT NULL,
                    room        TEXT,
                    source      TEXT,
                    title       TEXT,
                    text        TEXT NOT NULL,
                    embedding   vector(768),
                    metadata    JSONB DEFAULT '{}'::jsonb,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            for idx in (
                "CREATE INDEX IF NOT EXISTS idx_memories_wing ON memories(wing)",
                "CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source)",
                "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops)",
            ):
                cur.execute(idx)
            c.commit()

    # ---- write ----
    def _assert_writer(self, role: str, scope: str) -> None:
        if scope == "project":
            if role not in PROJECT_WRITERS:
                raise PermissionDenied(f"role={role} cannot write project memory")
            return
        if scope == "own":
            if role not in ALL_ROLES:
                raise PermissionDenied(f"unknown role: {role}")
            return
        raise ValueError(f"scope must be 'project' or 'own', got {scope!r}")

    def _wing_for(self, role: str, scope: str) -> str:
        return "project" if scope == "project" else f"agent/{role}"

    def remember(self, role: str, scope: str, text: str, *,
                 title: str | None = None, room: str | None = None,
                 source: str | None = None, metadata: dict[str, Any] | None = None) -> int:
        self._assert_writer(role, scope)
        wing = self._wing_for(role, scope)
        vec = embed(text, model=self.embed_model)
        with self._connect() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO memories(wing, room, source, title, text, embedding, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s::vector, %s::jsonb) RETURNING id",
                (wing, room, source, title, text, _vec_literal(vec),
                 json.dumps(metadata or {})),
            )
            rid = cur.fetchone()[0]
            c.commit()
            return rid

    def bulk_insert(self, rows: list[dict]) -> int:
        """rows = [{wing, room?, source?, title?, text, metadata?}]. Embeds each text."""
        n = 0
        with self._connect() as c, c.cursor() as cur:
            for r in rows:
                vec = embed(r["text"], model=self.embed_model)
                cur.execute(
                    "INSERT INTO memories(wing, room, source, title, text, embedding, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector, %s::jsonb)",
                    (r["wing"], r.get("room"), r.get("source"), r.get("title"),
                     r["text"], _vec_literal(vec), json.dumps(r.get("metadata") or {})),
                )
                n += 1
                if n % 100 == 0:
                    c.commit()
            c.commit()
        return n

    # ---- read ----
    def search(self, role: str, query: str, *, scope: str = "auto",
               limit: int = 5, wing: str | None = None) -> list[dict]:
        qvec = embed(query, model=self.embed_model)
        with self._connect() as c, c.cursor() as cur:
            if wing:
                cur.execute(
                    "SELECT id, wing, room, source, title, text, "
                    "       1 - (embedding <=> %s::vector) AS score "
                    "FROM memories WHERE wing = %s "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec_literal(qvec), wing, _vec_literal(qvec), limit),
                )
            elif scope == "project":
                cur.execute(
                    "SELECT id, wing, room, source, title, text, "
                    "       1 - (embedding <=> %s::vector) AS score "
                    "FROM memories WHERE wing = 'project' "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec_literal(qvec), _vec_literal(qvec), limit),
                )
            elif scope == "own":
                cur.execute(
                    "SELECT id, wing, room, source, title, text, "
                    "       1 - (embedding <=> %s::vector) AS score "
                    "FROM memories WHERE wing = %s "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec_literal(qvec), f"agent/{role}", _vec_literal(qvec), limit),
                )
            else:  # auto: project + own
                cur.execute(
                    "SELECT id, wing, room, source, title, text, "
                    "       1 - (embedding <=> %s::vector) AS score "
                    "FROM memories WHERE wing IN ('project', %s) "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec_literal(qvec), f"agent/{role}", _vec_literal(qvec), limit),
                )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def wing_counts(self) -> dict[str, int]:
        with self._connect() as c, c.cursor() as cur:
            cur.execute("SELECT wing, COUNT(*) FROM memories GROUP BY wing ORDER BY 2 DESC")
            return dict(cur.fetchall())
