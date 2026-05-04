"""Cross-ticket pattern mining — promote 3+ similar Doer fixes to
T3 patterns automatically.

Heuristic (KISS, no clustering libs):
1. Read recent ``doer_outcome`` facts from T3
   (``patterns/doer-success`` wing) over the last N days.
2. Group by overlapping ``files`` set (Jaccard >= 0.5) AND same
   ``stop_reason``.
3. When a group has >= 3 outcomes, emit a synthesised
   "pattern" T3 fact under ``patterns/auto-promoted/<topic>``
   summarising the common file-set + a count of supporting tickets.
4. Mark each contributing fact's metadata with
   ``promoted_to: <new_id>`` so we don't double-promote.

Run as a one-shot from systemd timer or ``aiforge memory mine``.
Defaults:
- ``AIFORGE_PATTERN_DAYS=30`` — lookback window
- ``AIFORGE_PATTERN_MIN_SUPPORT=3``

Public surface:
- ``run() -> dict``
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any


def run() -> dict:
    """Scan + promote. Returns ``{scanned, groups, promoted}``."""
    days = int(os.environ.get("AIFORGE_PATTERN_DAYS", "30"))
    min_support = int(os.environ.get("AIFORGE_PATTERN_MIN_SUPPORT", "3"))

    facts = _recent_doer_facts(days_back=days)
    if not facts:
        return {"scanned": 0, "groups": 0, "promoted": 0}

    groups = _group_by_file_overlap(facts)
    promoted = 0
    for key, members in groups.items():
        if len(members) < min_support:
            continue
        if any(m.get("metadata", {}).get("promoted_to") for m in members):
            continue
        new_fact = _synthesise(key, members)
        new_id = _persist_pattern(new_fact)
        if new_id:
            _mark_promoted(members, new_id)
            promoted += 1
    return {
        "scanned": len(facts),
        "groups": len(groups),
        "promoted": promoted,
    }


# ───────── helpers ────────────────────────────────────────────────


def _recent_doer_facts(days_back: int) -> list[dict]:
    """Read T3 facts under patterns/doer-success or patterns/doer-failure."""
    try:
        from aiforge_core.legacy.rag.neo4j_memory import driver  # type: ignore
    except ImportError:
        return []
    cy = (
        "MATCH (m:Memory) "
        "WHERE m.tier = 't3' "
        "  AND (m.wing STARTS WITH 'patterns/doer-success' "
        "       OR m.wing STARTS WITH 'patterns/doer-failure') "
        "  AND m.created_at > datetime() - duration({days: $days}) "
        "RETURN m.id AS id, m.text AS text, m.wing AS wing, "
        "       m.metadata AS metadata, m.created_at AS created_at "
        "ORDER BY m.created_at DESC "
        "LIMIT 1000"
    )
    with driver().session() as sess:
        return [
            {
                "id": r["id"], "text": r["text"], "wing": r["wing"],
                "metadata": (r["metadata"] or {}),
                "created_at": r["created_at"],
            }
            for r in sess.run(cy, days=days_back)
        ]


def _group_by_file_overlap(
    facts: list[dict], *, jaccard_threshold: float = 0.5,
) -> dict[str, list[dict]]:
    """Greedy single-pass grouping. Returns ``key→members``."""
    groups: dict[str, list[dict]] = defaultdict(list)
    seen_keys: list[set[str]] = []

    for fact in facts:
        files = set(fact.get("metadata", {}).get("files") or [])
        stop = (fact.get("metadata", {}).get("stop_reason") or "")
        if not files:
            continue
        # Find best-matching existing key by Jaccard.
        best_idx = -1
        best_score = 0.0
        for i, key_files in enumerate(seen_keys):
            inter = len(files & key_files)
            if inter == 0:
                continue
            jac = inter / len(files | key_files)
            if jac > best_score and jac >= jaccard_threshold:
                best_score = jac
                best_idx = i
        if best_idx == -1:
            seen_keys.append(set(files))
            best_idx = len(seen_keys) - 1
        key = f"{stop}|{'|'.join(sorted(seen_keys[best_idx]))[:200]}"
        groups[key].append(fact)
    return dict(groups)


def _synthesise(key: str, members: list[dict[str, Any]]) -> dict:
    files: set[str] = set()
    tickets: list[str] = []
    success_count = 0
    fail_count = 0
    for m in members:
        meta = m.get("metadata") or {}
        files.update(meta.get("files") or [])
        if meta.get("ticket"):
            tickets.append(meta["ticket"])
        if meta.get("worked"):
            success_count += 1
        else:
            fail_count += 1
    text = (
        f"Auto-promoted pattern · {len(members)} similar Doer outcomes\n"
        f"Files: {', '.join(sorted(files)[:10])}\n"
        f"Outcomes: {success_count} success / {fail_count} failure\n"
        f"Tickets: {', '.join(tickets[:8])}"
    )
    return {
        "tier": "t3",
        "wing": "patterns/auto-promoted",
        "kind": "pattern",
        "text": text,
        "metadata": {
            "support_count": len(members),
            "success_count": success_count,
            "fail_count": fail_count,
            "files": sorted(files)[:20],
            "source_tickets": tickets[:20],
        },
    }


def _persist_pattern(fact: dict) -> str | None:
    # runtime.memory removed — persist is a no-op stub returning None.
    return None


def _mark_promoted(members: list[dict], new_id: str) -> None:
    try:
        from aiforge_core.legacy.rag.neo4j_memory import driver  # type: ignore
    except ImportError:
        return
    cy = (
        "UNWIND $ids AS id "
        "MATCH (m:Memory {id: id}) "
        "SET m.metadata = m.metadata + {promoted_to: $new_id}"
    )
    ids = [m["id"] for m in members if m.get("id")]
    if not ids:
        return
    with driver().session() as sess:
        sess.run(cy, ids=ids, new_id=new_id)
