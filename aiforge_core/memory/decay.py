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
    """Archive stale facts. Returns ``{postgres, neo4j, afm}`` counters.

    The ``afm`` counter is the AiForgeMemory-side ``Observation_v2``
    + ``Decision_v2`` archival (added 2026-05-23 after the
    `(repo, text)` dedupe in AFM landed). The legacy ``neo4j`` path
    targets the older ``:Memory`` label and stays for backward compat.
    """
    age = int(os.environ.get("AIFORGE_DECAY_AGE_DAYS", "90"))
    batch = int(os.environ.get("AIFORGE_DECAY_BATCH", "500"))
    out = {"postgres": 0, "neo4j": 0, "afm": 0, "errors": []}

    try:
        out["postgres"] = _decay_postgres(age, batch)
    except Exception as exc:
        out["errors"].append(f"postgres: {exc}")
    try:
        out["neo4j"] = _decay_neo4j(age, batch)
    except Exception as exc:
        out["errors"].append(f"neo4j: {exc}")
    try:
        out["afm"] = _decay_afm(age, batch)
    except Exception as exc:
        out["errors"].append(f"afm: {exc}")
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
    """Legacy Neo4j path — :Memory{created_at, hit_count, status}.

    Kept for backward compat with pre-AFM memory nodes. New writes
    target ``:Observation_v2`` / ``:Decision_v2`` (handled by
    :func:`_decay_afm`).
    """
    try:
        from aiforge_core.memory.rag.neo4j_memory import driver  # type: ignore
    except ImportError:
        return 0
    cy = (
        "MATCH (m:Memory) "
        # Parenthesize the status clause — without it, AND binds tighter than
        # OR and the rule degrades to "status IS NULL OR (active AND hit=0 AND
        # old)", archiving EVERY status-less legacy node regardless of hits/age.
        "WHERE (m.status IS NULL OR m.status = 'active') "
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


def _decay_afm(age_days: int, batch: int) -> int:
    """AFM-side archival of ``Observation_v2`` + ``Decision_v2``.

    Archives nodes where:
    - ``status`` is null or ``'active'``
    - ``seen_count`` ≤ 1 (i.e. emitted once, never re-hit by the
      ``(repo, text)`` dedupe path — strong signal it wasn't reused)
    - ``created_at`` older than ``$days``
    - ``last_seen_at`` is null OR older than ``$days`` too — so a fact
      that got bumped recently still survives even if its creation
      timestamp is ancient.

    Hot facts (seen_count > 1) are kept regardless of age. This is
    intentionally distinct from the legacy ``:Memory`` rule that keyed
    on ``hit_count``; AFM never wrote ``hit_count`` so we couldn't
    reuse the same path.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return 0

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        cy = (
            "MATCH (o) "
            "WHERE (o:Observation_v2 OR o:Decision_v2) "
            "  AND (o.status IS NULL OR o.status = 'active') "
            "  AND coalesce(o.seen_count, 1) <= 1 "
            "  AND o.created_at IS NOT NULL "
            "  AND o.created_at < datetime() - duration({days: $days}) "
            "  AND (o.last_seen_at IS NULL "
            "       OR o.last_seen_at < datetime() - duration({days: $days})) "
            "WITH o LIMIT $batch "
            "SET o.status = 'archived', "
            "    o.archived_at = datetime() "
            "RETURN count(o) AS n"
        )
        with drv.session() as sess:
            rec = sess.run(cy, days=age_days, batch=batch).single()
            return int((rec or {"n": 0}).get("n", 0))
    finally:
        try:
            drv.close()
        except Exception:
            pass
