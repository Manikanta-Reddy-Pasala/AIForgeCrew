"""Memory access for the v5 orchestrator.

Wraps the existing `aiforge_core.store_v2.Store` + `retrieval.retrieve_for_role`
so tools.py can expose a single `search` and `retain_fact` surface to the LLM.

All tiers live in one Postgres table (`memories`) on the aiforge DB:
  T1 episodic  wing = ticket/<identifier>
  T2 canon     wing = rules/*     (hindsight migration target)
  T3 skills    wing = skills/*, patterns/*
  T4 code      wing = code/<repo>, code/claude-memory
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aiforge_core.store_v2 import Store
from aiforge_core.retrieval import retrieve_for_role, Hit


@dataclass
class SearchResult:
    tier: str
    wing: str
    source: str | None
    text: str
    score: float
    metadata: dict


class Memory:
    """Thin adapter so tools.py never touches the Store class directly."""

    def __init__(self) -> None:
        self._store = Store()
        self._store.ensure_schema()

    # ── read path ────────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        *,
        role: str = "sr_developer",
        parent_id: str | None = None,
        top_k: int | None = None,
        wing_prefix: str | None = None,  # accepted for prompt UX; NOT post-filtered
    ) -> list[SearchResult]:
        """Role-tuned retrieval across all tiers.

        `wing_prefix` kept in the signature for backward-compatibility
        with agent tool-call JSON, but the retrieval policy already
        applies per-tier wing filters inside `retrieve_for_role` (see
        ROLE_POLICIES in retrieval.py). We do NOT post-filter hits on
        `metadata.wing` here — hit.metadata doesn't guarantee a `wing`
        key, which silently emptied results in v5 early tests.
        """
        hits: list[Hit] = retrieve_for_role(
            self._store, role=role, query=query, parent_id=parent_id,
        )
        if top_k is not None:
            hits = hits[:top_k]
        return [
            SearchResult(
                tier=(h.tier or "?"),
                wing=(h.metadata or {}).get("wing", "?"),
                source=h.source,
                text=h.text,
                score=h.score,
                metadata=h.metadata or {},
            )
            for h in hits
        ]

    # ── write path ───────────────────────────────────────────────────────
    def retain_fact(
        self,
        text: str,
        *,
        tier: str = "t2",
        wing: str = "rules/canon",
        kind: str = "fact",
        source: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Persist a net-new fact into the memories table.

        Defaults: tier=t2 (canon) + wing=rules/canon, i.e. the bucket we
        migrated hindsight into. Fact Extract typically writes to
        tier=t3 + wing='patterns/<topic>' for recipe-style facts.
        """
        if not text or not text.strip():
            raise ValueError("retain_fact: text is empty")
        if tier not in ("t1", "t2", "t3", "t4"):
            raise ValueError(f"retain_fact: bad tier {tier!r}")

        # Store.upsert_memory is the canonical writer on the store; fall
        # back to a direct insert if the helper doesn't exist on this
        # Store version.
        meta = {"source": source or "agent.retain", **(metadata or {})}
        if hasattr(self._store, "upsert_memory"):
            return self._store.upsert_memory(
                tier=tier, wing=wing, kind=kind, text=text[:10_000],
                metadata=meta, source=source,
            )
        # Fallback — raw SQL via store's connection helper.
        with self._store._connect() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (tier, wing, kind, text, metadata, source)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id;
                """,
                (tier, wing, kind, text[:10_000], _json(meta), source),
            )
            row_id = cur.fetchone()[0]
            c.commit()
        # Best-effort embedding — handled out of band by a nightly backfill
        # or on next search (we only emit if embed sidecar is live).
        _maybe_embed(self._store, row_id, text)
        return row_id


def _json(obj: dict) -> str:
    import json as _j
    return _j.dumps(obj, ensure_ascii=False)


def _maybe_embed(store: Store, row_id: int, text: str) -> None:
    """Embed + update `embedding` column. Swallows errors — retain is
    write-forward; embedding backfill is idempotent."""
    try:
        from aiforge_core.embed import embed
        vec = embed(text[:8000])
        with store._connect() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE memories SET embedding=%s WHERE id=%s",
                (vec, row_id),
            )
            c.commit()
    except Exception:
        pass
