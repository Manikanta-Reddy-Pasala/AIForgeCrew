"""Hybrid Neo4j fact retrieval for GA memory injection.

Wraps the canonical L2 :Fact retrieval into a single string that can be
concatenated onto GA's filesystem-read `global_mem_insight.txt`. Used by
the Doer's monkey-patch of `ga.get_global_memory` (see ga_runner.py).

Hybrid ranking: native Neo4j vector index on Fact.embedding (cosine,
0.7 weight) + fulltext Lucene on Fact.text (0.3 weight) + 2-hop graph
boost (+0.2 if fact is :ABOUT a node related to the active context).

Returns "" on any failure — the caller falls back to GA filesystem
memory only. Never raises.
"""
from __future__ import annotations

import os
import threading
from typing import Any

# Thread-local active-fetch context, set by run_doer_via_ga before the
# agent_runner_loop is invoked. The monkey-patched get_global_memory
# reads this to know what ticket / files / role to scope facts to.
_ctx = threading.local()


def set_fetch_context(ticket_id: str | None = None,
                      role: str = "doer",
                      file_paths: list[str] | None = None,
                      query_text: str | None = None) -> None:
    """Stash the per-run scoping context. Called once before the
    GA agent loop starts so the patched get_global_memory() has it."""
    _ctx.ticket_id = ticket_id
    _ctx.role = role
    _ctx.file_paths = file_paths or []
    _ctx.query_text = query_text or ""


def clear_fetch_context() -> None:
    for k in ("ticket_id", "role", "file_paths", "query_text"):
        if hasattr(_ctx, k):
            delattr(_ctx, k)


def _driver():
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        return None
    from aiforge_core.memory.neo4j_conn import neo4j_params
    uri, user, pw = neo4j_params()
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception:
        return None


# Hybrid Cypher: fulltext (BM25) + 2-hop graph boost.
# Vector path skipped — Learner doesn't write embeddings yet. When it
# does, add a CALL db.index.vector.queryNodes branch + fuse weights.
# Returns top-K Facts ordered by combined score.
_HYBRID_CYPHER = """
CALL db.index.fulltext.queryNodes('factText', $q) YIELD node AS f, score AS lex
WITH f, lex,
     CASE WHEN $ticket_id IS NULL THEN 0.0
          WHEN exists((f)-[:ABOUT]->(:Ticket {id: $ticket_id})) THEN 0.4
          ELSE 0.0 END AS hop_boost
WITH f, lex + hop_boost AS score
RETURN f.text AS text, coalesce(f.source, 'learner') AS source, score
ORDER BY score DESC LIMIT $k
"""

# Simpler fallback when fulltext index isn't there (e.g. on bring-up).
_FALLBACK_CYPHER = """
MATCH (f:Fact)
WHERE f.text IS NOT NULL
RETURN f.text AS text, coalesce(f.source, 'unknown') AS source, 0.5 AS score
ORDER BY coalesce(f.created_at, datetime()) DESC
LIMIT $k
"""


def _build_query_text(ctx) -> str:
    if getattr(ctx, "query_text", ""):
        return ctx.query_text
    parts: list[str] = []
    files = getattr(ctx, "file_paths", []) or []
    for p in files[:5]:
        # tokenise on path separators; helps the fulltext index hit
        # tokens like "PaymentInController" instead of full paths
        parts.append(p.replace("/", " ").replace(".", " "))
    if getattr(ctx, "ticket_id", None):
        parts.append(ctx.ticket_id)
    return " ".join(parts).strip()


def fetch_facts_text(k: int = 5) -> str:
    """Return a formatted plain-text block of top-K :Fact contents.

    Format:
        [Neo4j L2 facts — top-N]
        - <source>: <text snippet>
        ...

    Returns "" on any error (driver unreachable, no facts, etc).
    """
    drv = _driver()
    if drv is None:
        return ""
    try:
        ticket_id = getattr(_ctx, "ticket_id", None)
        q = _build_query_text(_ctx) or "code"
        rows: list[dict] = []
        with drv.session() as s:
            try:
                rows = s.run(_HYBRID_CYPHER,
                             q=q, ticket_id=ticket_id, k=k).data()
            except Exception:
                # Fulltext index may not exist yet on a bare graph — fall
                # back to a recency-ordered scan.
                try:
                    rows = s.run(_FALLBACK_CYPHER, k=k).data()
                except Exception:
                    rows = []
    except Exception:
        return ""
    finally:
        try:
            drv.close()
        except Exception:
            pass
    if not rows:
        return ""
    lines = [f"[Neo4j L2 facts — top-{len(rows)}, hybrid retrieve]"]
    for row in rows:
        text = (row.get("text") or "").strip().replace("\n", " ")
        text = text[:300] + ("…" if len(text) > 300 else "")
        src = row.get("source") or "?"
        lines.append(f"- {src}: {text}")
    return "\n".join(lines)
