"""Memory decay — archive stale, never-reused facts.

Ported from AIForgeCrew's ``aiforge_core.memory.decay._decay_afm`` so
the scheduler daemon (and ``aiforge-memory decay``) own the job instead
of an external cron.

Rule (KISS): archive ``Observation_v2`` nodes where
- ``status`` is null or ``'active'``
- ``seen_count`` ≤ 1 — emitted once, never re-hit by the (repo, text)
  dedupe path; strong signal it wasn't reused
- ``created_at`` older than ``max_age_days``
- ``last_seen_at`` is null OR older than ``max_age_days`` too — a fact
  bumped recently survives even if its creation timestamp is ancient

Archived ≠ deleted: ``status='archived'`` + ``archived_at`` stamp, so
recall (which keeps ``status IS NULL OR status = 'active'``) drops it
but the node stays recoverable.

Env:
    AIFORGE_DECAY_BATCH=500   per-transaction cap (avoids long Cypher tx)

Public surface:
    run_decay(driver, *, max_age_days=30) -> dict
"""
from __future__ import annotations

import os

# Observation_v2 ONLY. Decision_v2 deliberately excluded: decisions are
# durable architecture records with NO touch path (nothing bumps their
# seen_count/last_seen_at on recall), so age-based decay would archive
# EVERY decision older than the threshold — and _decisions_for filters
# archived nodes out of bundles. Revisit only if decisions ever gain a
# touch-on-recall.
_DECAY_CY = """
MATCH (o:Observation_v2)
WHERE (o.status IS NULL OR o.status = 'active')
  AND coalesce(o.seen_count, 1) <= 1
  AND o.created_at IS NOT NULL
  AND o.created_at < datetime() - duration({days: $days})
  AND (o.last_seen_at IS NULL
       OR o.last_seen_at < datetime() - duration({days: $days}))
WITH o LIMIT $batch
SET o.status = 'archived',
    o.archived_at = datetime()
RETURN count(o) AS n
"""

# Hard ceiling on batch loops per run — guards against a pathological
# graph keeping a tick busy forever (50 × 500 = 25k archives/run).
_MAX_BATCHES = 50


def run_decay(driver, *, max_age_days: int = 30) -> dict:
    """Archive stale facts in batches until none remain (or the batch
    ceiling hits). Returns ``{"archived": n, "max_age_days": days}``."""
    batch = int(os.environ.get("AIFORGE_DECAY_BATCH", "500"))
    archived = 0
    with driver.session() as sess:
        for _ in range(_MAX_BATCHES):
            rec = sess.run(_DECAY_CY, days=max_age_days,
                           batch=batch).single()
            n = int((rec or {"n": 0}).get("n", 0))
            archived += n
            if n < batch:
                break
    return {"archived": archived, "max_age_days": max_age_days}
