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
Safe to call when the Postgres backend isn't enabled (soft-fails).

Public surface:
- ``run() -> dict`` — counts archived per backend
"""
from __future__ import annotations

import os


def run() -> dict:
    """Archive stale facts. Returns ``{postgres}`` counter."""
    age = int(os.environ.get("AIFORGE_DECAY_AGE_DAYS", "90"))
    batch = int(os.environ.get("AIFORGE_DECAY_BATCH", "500"))
    out = {"postgres": 0, "errors": []}

    try:
        out["postgres"] = _decay_postgres(age, batch)
    except Exception as exc:
        out["errors"].append(f"postgres: {exc}")
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
            -- Guard the ::int cast — a non-numeric hit_count would otherwise
            -- abort the whole decay transaction.
            AND COALESCE(CASE WHEN metadata->>'hit_count' ~ '^[0-9]+$'
                              THEN (metadata->>'hit_count')::int END, 0) = 0
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
