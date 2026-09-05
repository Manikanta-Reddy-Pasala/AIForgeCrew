"""The individual memory sources unified_query fans out to: ticket brief,
generic MCP calls, external docs lookup, the AiForgeMemory ContextBundle,
prior chat sessions, and cross-repo links. All lazy-import their backends
and soft-fail; no cross-group imports so no import cycles."""
from __future__ import annotations

import os
from typing import Any


def _ticket_brief(_identifier: str) -> dict | None:
    """Fetch a ticket brief — always None on this build, and that is the point.

    It used to read local Postgres first and fall back to the graph_mcp
    ticket_brief proxy. BOTH backends went with the SQLite-only build, so both
    branches were dead: the local read returned None and the proxy could never
    return rows. Keeping them written out as live-looking calls read as a
    working lookup to a human and as an always-false condition to an analyser,
    and neither reading was useful.

    So the body says what is true. The CALLER is untouched — recall still asks
    for a brief and still handles None — so restoring a backend is this
    function's body plus ``_unpack_mcp_rows``, which is still here and still
    used by the sym_lookup/find_doc sources.
    """
    return None


def _ticket_local(_identifier: str) -> dict | None:
    """Direct read from a local ticket store — no-op, kept for the same reason
    ``_ticket_brief`` is: this build has no local ticket backend to read."""
    return None


def _mcp_call(_tool: str, _args: dict) -> Any:
    """The graph_rag MCP backend was removed (SQLite-only build), so the graph
    tool-call sources (sym_lookup / find_doc / related_memories) are no-ops."""
    return None


def _docs_lookup(library: str, text: str, *, top_k: int) -> list[dict]:
    from aiforge_core.indexing.docs_index import lookup_doc
    chunks = lookup_doc(library, text, top_k=top_k) or []
    out: list[dict] = []
    for c in chunks:
        out.append({
            "text": c.get("text"),
            "score": float(c.get("score") or 0.5),
            "source_uri": c.get("url"),
        })
    return out


def _unpack_mcp_rows(raw: Any) -> list[dict]:
    """MCP results come as a stringified blob from the graph helper.
    Normalise to a list of ``{text, score?, source_uri?}`` dicts."""
    import json as _j
    if isinstance(raw, str):
        try:
            data = _j.loads(raw)
        except Exception:
            return [{"text": raw[:1000], "score": 0.5}]
    else:
        data = raw
    if isinstance(data, dict):
        # Common shape: {result: {content: [{type:'text', text:'...'}]}}
        content = data.get("content") or data.get("result", {}).get("content")
        if isinstance(content, list):
            return [
                {"text": (b.get("text") or "")[:1500], "score": 0.7}
                for b in content if isinstance(b, dict)
            ]
        # Fallback: flat dict → one row
        return [{"text": _j.dumps(data, ensure_ascii=False)[:1500],
                 "score": 0.5}]
    if isinstance(data, list):
        return [
            {"text": (
                d.get("text") or _j.dumps(d, ensure_ascii=False)[:1500])[:1500],
             "score": float(d.get("score") or 0.5),
             "source_uri": d.get("uri")}
            for d in data if isinstance(d, (dict, str))
        ]
    return [{"text": str(data)[:1500], "score": 0.5}]


def _global_vector_recall(_text: str, *, limit: int,
                          repo: str | None = None) -> list[dict]:
    """Global observation recall — was backed by an optional graph vector
    index that has been removed (SQLite-only build). The embedded SQLite
    vector recall (source 1) now owns semantic recall, so this is a no-op
    that returns []."""
    # unused, deliberately: the graph vector index this took is gone (SQLite-only build); the signature stays for the caller.
    del limit, repo
    return []


def _chunk_score() -> float:
    """Code chunks are raw RAG evidence, DEMOTED below the curated sources
    (repo_map/conventions/notes and the OKR-DAG goal context): with a
    goal-oriented memory, a wall of code chunks should not dominate recall.
    Env-tunable AIFORGE_UMEM_CHUNK_SCORE (default 0.4, was 0.85)."""
    try:
        return max(0.0, min(1.0, float(
            os.environ.get("AIFORGE_UMEM_CHUNK_SCORE", "0.4"))))
    except (TypeError, ValueError):
        return 0.4


def _chunk_rows(chunks, repo: str) -> list[dict]:
    score = _chunk_score()
    out = []
    for c in (chunks or [])[:5]:
        path = c.get("file_path") or ""
        body = (c.get("text") or "").strip()
        if not (path and body):
            continue
        out.append({
            # Per-chunk group (not a shared "afm:chunk") so _diversify's
            # per-group cap doesn't drop 5 doer-evidence chunks down to 3.
            "text": f"[afm/chunk {path}]\n{body[:800]}",
            "group": f"afm:chunk:{path}",
            "score": score,
            "source_uri": f"afm://{repo}/{path}",
        })
    return out


def _titled_rows(items, *, repo: str, label: str, group: str, score: float,
                 default_title: str, cap: int, uri_kind: str) -> list[dict]:
    """Notes and docs differ only in their label, weight and default title."""
    out = []
    for it in (items or [])[:3]:
        body = (it.get("body") or "").strip()
        if not body:
            continue
        title = it.get("title") or default_title
        out.append({
            "text": f"[afm/{label} {title}]\n{body[:cap]}",
            "group": group,
            "score": score,
            "source_uri": (it.get("url") or
                           f"afm://{repo}/{uri_kind}/{it.get('id', '')}"),
        })
    return out


def _observation_rows(observations, repo: str) -> list[dict]:
    """Vector-recalled observations (typically agent learnings)."""
    out = []
    for o in (observations or [])[:3]:
        body = (o.get("text") or "").strip()
        if not body:
            continue
        kind = o.get("kind") or "observation"
        out.append({
            "text": f"[afm/{kind}]\n{body[:500]}",
            "group": "afm:observation",
            "score": float(o.get("score") or 0.55),
            "source_uri": f"afm://{repo}/observation/{o.get('id', '')}",
        })
    return out


def _chat_sessions(text: str, *, limit: int,
                   exclude_session: int | None = None) -> list[dict]:
    """Flatten prior chat-session message hits into ranked rows (gap F3).

    ``chat_store.search_messages`` returns ``[{session_id, session_title,
    role, content, created_at}]`` already ranked by relevance. We map each
    to a unified hit tagged ``source="chat"`` with a descending raw score so
    the in-source order survives min-max normalization (equal scores would
    collapse to a single value and lose the ranking). Per-session ``group``
    lets ``_diversify`` cap a single chatty session. Soft-fail → []."""
    from aiforge_core.runtime import chat_store
    # Exclude the CURRENT session so a live turn's own messages don't return
    # as "prior chat" (gap M4). Soft-fail if the backend lacks the kwarg.
    try:
        rows = chat_store.search_messages(
            text, limit=limit, exclude_session=exclude_session) or []
    except TypeError:
        rows = chat_store.search_messages(text, limit=limit) or []
    out: list[dict] = []
    n = len(rows)
    for i, r in enumerate(rows):
        content = (r.get("content") or "").strip()
        if not content:
            continue
        sid = r.get("session_id")
        title = r.get("session_title") or "chat"
        role = r.get("role") or "?"
        # Descending raw score preserves search rank through normalization.
        score = 1.0 - (i / max(1, n))
        out.append({
            "text": f"[chat {title} · {role}] {content[:600]}",
            "source": "chat",
            "score": score,
            "group": f"chat:{sid}",
            # Per-MESSAGE uri (index-qualified) — distinct messages in one
            # session are NOT the same doc, so dedup must not collapse them.
            "source_uri": f"chat://{sid}/{i}",
        })
    return out
