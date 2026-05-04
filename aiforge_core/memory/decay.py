"""Memory decay / expiry — periodic cron over the memories table.

Rule (KISS):
- Facts with ``hit_count = 0`` AND older than N days are archived
  (status=archived, NOT deleted — recoverable).
- Facts with hit_count >= 1 are kept regardless of age.
- Archived facts are filtered out of search results by the retrieval
  layer (existing role policy honors ``status``).

Defaults via env:
- ``AIFORGE_DECAY_AGE_DAYS=90`` — minimum age before archival
- ``AIFORGE_DECAY_BATCH=500`` — per-run cap (avoids long Cypher tx)

Run as a one-shot from systemd timer or ``aiforge memory decay`` CLI.
Postgres + Neo4j paths handled separately; both safe to call when
either backend isn't enabled.

Public surface:
- ``run() -> dict`` — counts archived per backend
"""
from __future__ import annotations

import os


def run() -> dict:
    """Archive stale facts. Returns ``{postgres, neo4j}`` counters."""
    age = int(os.environ.get("AIFORGE_DECAY_AGE_DAYS", "90"))
    batch = int(os.environ.get("AIFORGE_DECAY_BATCH", "500"))
    out = {"postgres": 0, "neo4j": 0, "errors": []}

    try:
        out["postgres"] = _decay_postgres(age, batch)
    except Exception as exc:
        out["errors"].append(f"postgres: {exc}")
    try:
        out["neo4j"] = _decay_neo4j(age, batch)
    except Exception as exc:
        out["errors"].append(f"neo4j: {exc}")
    return out


# ───────── backends ────────────────────────────────────────────────


def _decay_postgres(age_days: int, batch: int) -> int:
    """UPDATE memories SET status='archived' WHERE created_at < NOW()
    - INTERVAL 'N days' AND COALESCE(metadata->>'hit_count','0') = '0'
    AND COALESCE(status,'active') = 'active' LIMIT batch."""
    import psycopg
    from aiforge_core.config.env import AIFORGE_DSN
    sql = """
        WITH stale AS (
          SELECT id FROM memories
          WHERE created_at < NOW() - (%s || ' days')::interval
            AND COALESCE((metadata->>'hit_count')::int, 0) = 0
            AND COALESCE(status, 'active') = 'active'
          ORDER BY created_at ASC
          LIMIT %s
        )
        UPDATE memories SET status = 'archived'
        WHERE id IN (SELECT id FROM stale)
        RETURNING id;
    """
    with psycopg.connect(AIFORGE_DSN, connect_timeout=5) as c, \
         c.cursor() as cur:
        # Add status column if missing — idempotent.
        cur.execute(
            "ALTER TABLE memories "
            "ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
        )
        cur.execute(sql, (age_days, batch))
        rows = cur.fetchall()
        c.commit()
        return len(rows)


def _decay_neo4j(age_days: int, batch: int) -> int:
    """Neo4j path — :Memory{created_at, hit_count, status}."""
    try:
        from aiforge_core.memory.rag.neo4j_memory import driver  # type: ignore
    except ImportError:
        return 0
    cy = (
        "MATCH (m:Memory) "
        "WHERE m.status IS NULL OR m.status = 'active' "
        "  AND coalesce(m.hit_count, 0) = 0 "
        "  AND m.created_at IS NOT NULL "
        "  AND m.created_at < datetime() - duration({days: $days}) "
        "WITH m LIMIT $batch "
        "SET m.status = 'archived' "
        "RETURN count(m) AS n"
    )
    with driver().session() as sess:
        rec = sess.run(cy, days=age_days, batch=batch).single()
        return int((rec or {"n": 0}).get("n", 0))
