"""Unified memory query — one tool, all sources, ranked + merged.

KISS: single ``query(text, *, ticket=None, role=None, limit=8)`` call
fans out to every memory backend in parallel and returns one ranked
list. Replaces the 3-tool chain ``search_memory → ticket_brief →
related_memories`` (and often ``sym_lookup`` / ``find_doc`` on top).

Sources merged (each contributes, soft-fail individually):
1. Memory.search — Postgres / Neo4j hybrid retrieval
2. ticket_brief (if ``ticket`` looks like an identifier)
3. related_memories MCP — Neo4j graph hops
4. sym_lookup MCP — code symbol + signature
5. find_doc MCP — markdown / SOP files
6. docs_index.lookup_doc — external library docs (top library guessed
   from query: spring/react/mongodb/...)

Ranking: each source emits ``score`` in [0, 1]; we normalise per
source and take top-K by combined score (per-source weight tunable
via ``AIFORGE_UMEM_WEIGHT_<source>``).

Public surface:
- ``query(text, *, ticket=None, role=None, limit=8) -> dict``
"""
from __future__ import annotations

import os
import re
from typing import Any


_DEFAULT_WEIGHTS = {
    "memory":     1.0,
    "ticket":     1.2,
    "related":    0.8,
    "symbol":     0.9,
    "doc":        0.6,
    "external":   0.5,
}


_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d+)\b")


def query(
    text: str, *,
    ticket: str | None = None,
    role: str | None = None,
    limit: int = 8,
) -> dict:
    """Unified retrieval. Returns ``{hits, used_sources, errors}``."""
    if not text.strip():
        return {"hits": [], "used_sources": [], "errors": []}

    weights = _resolve_weights()
    used: list[str] = []
    errors: list[str] = []
    raw_hits: list[dict] = []

    # 1) Memory hybrid search
    try:
        rows = _memory_search(text, role=role, top_k=limit)
        used.append("memory")
        raw_hits.extend(_tag(rows, source="memory", weight=weights["memory"]))
    except Exception as exc:
        errors.append(f"memory: {exc}")

    # 2) Ticket brief — explicit ticket OR auto-detected token
    auto_ticket = ticket or (_TICKET_RE.search(text) or [None])[0]
    if auto_ticket:
        try:
            row = _ticket_brief(auto_ticket)
            if row:
                used.append("ticket")
                raw_hits.append({
                    **row, "source": "ticket",
                    "score": 1.0 * weights["ticket"],
                })
        except Exception as exc:
            errors.append(f"ticket: {exc}")

    # 3) related_memories
    try:
        rows = _mcp_call("related_memories",
                         {"query": text, "top_k": limit})
        if rows:
            used.append("related")
            raw_hits.extend(
                _tag(_unpack_mcp_rows(rows),
                     source="related", weight=weights["related"]),
            )
    except Exception as exc:
        errors.append(f"related: {exc}")

    # 4) sym_lookup — only when query looks like a code symbol token
    if _looks_like_symbol(text):
        try:
            rows = _mcp_call("sym_lookup", {"name": _extract_symbol(text)})
            if rows:
                used.append("symbol")
                raw_hits.extend(
                    _tag(_unpack_mcp_rows(rows),
                         source="symbol", weight=weights["symbol"]),
                )
        except Exception as exc:
            errors.append(f"symbol: {exc}")

    # 5) find_doc — markdown / SOP search
    try:
        rows = _mcp_call("find_doc", {"query": text, "top_k": 3})
        if rows:
            used.append("doc")
            raw_hits.extend(
                _tag(_unpack_mcp_rows(rows),
                     source="doc", weight=weights["doc"]),
            )
    except Exception as exc:
        errors.append(f"doc: {exc}")

    # 6) external docs (library guessed from query)
    library = _guess_library(text)
    if library:
        try:
            rows = _docs_lookup(library, text, top_k=2)
            if rows:
                used.append(f"external:{library}")
                raw_hits.extend(
                    _tag(rows,
                         source=f"external:{library}",
                         weight=weights["external"]),
                )
        except Exception as exc:
            errors.append(f"external:{library}: {exc}")

    raw_hits.sort(key=lambda h: -float(h.get("score") or 0))
    return {
        "query": text,
        "hits": raw_hits[:limit],
        "used_sources": used,
        "errors": errors,
    }


def render(result: dict) -> str:
    """Pretty render for prompt injection. KISS bullets."""
    if not result.get("hits"):
        return "[unified_memory] no hits"
    lines = ["[unified_memory] sources used: " + ", ".join(
        result.get("used_sources") or ["none"])]
    for i, h in enumerate(result["hits"], 1):
        src = h.get("source") or "?"
        text = (h.get("text") or "")[:300].replace("\n", " ")
        lines.append(f"  {i}. [{src}|{h.get('score',0):.2f}] {text}")
    if result.get("errors"):
        lines.append("[errors] " + "; ".join(result["errors"]))
    return "\n".join(lines)


# ───────── helpers ────────────────────────────────────────────────


def _resolve_weights() -> dict:
    out = dict(_DEFAULT_WEIGHTS)
    for k in out:
        env = os.environ.get(f"AIFORGE_UMEM_WEIGHT_{k.upper()}")
        if env:
            try:
                out[k] = float(env)
            except ValueError:
                pass
    return out


def _memory_search(text: str, *, role: str | None, top_k: int) -> list[dict]:
    from aiforge_core.runtime.memory import Memory
    hits = Memory().search(text, role=role or "sr_developer", top_k=top_k)
    out: list[dict] = []
    for h in hits:
        out.append({
            "text": h.text,
            "tier": getattr(h, "tier", None),
            "wing": getattr(h, "wing", None),
            "score": float(getattr(h, "score", 0.5) or 0.5),
            "source_uri": getattr(h, "source", None),
        })
    return out


def _ticket_brief(identifier: str) -> dict | None:
    """Fetch ticket brief. Tries local Postgres first (canonical),
    falls back to graph_mcp ticket_brief (Linear/Jira proxy) when
    not found locally.
    """
    local = _ticket_local(identifier)
    if local:
        return local
    res = _mcp_call("ticket_brief", {"id": identifier})
    if not res:
        return None
    rows = _unpack_mcp_rows(res)
    if not rows:
        return None
    first = rows[0]
    first.setdefault("score", 1.0)
    return first


def _ticket_local(identifier: str) -> dict | None:
    """Direct read from local Postgres tickets + recent events.
    Bypasses graph_mcp's external-provider assumption."""
    try:
        import psycopg
        from aiforge_core.runtime.config import AIFORGE_DSN
        from psycopg.rows import dict_row
        with psycopg.connect(AIFORGE_DSN, connect_timeout=2,
                             row_factory=dict_row) as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, identifier, title, status, body, "
                "       to_char(created_at,'YYYY-MM-DD HH24:MI') AS created, "
                "       to_char(updated_at,'YYYY-MM-DD HH24:MI') AS updated "
                "FROM tickets WHERE identifier = %s LIMIT 1",
                (identifier,),
            )
            t = cur.fetchone()
            if not t:
                return None
            cur.execute(
                "SELECT agent_role, kind, body, "
                "       to_char(created_at,'HH24:MI:SS') AS ts "
                "FROM ticket_events WHERE ticket_id = %s "
                "ORDER BY created_at DESC LIMIT 8",
                (t["id"],),
            )
            ev = cur.fetchall() or []
        ev_lines = "\n".join(
            f"  [{(e['ts'] or '?'):8s} {(e['agent_role'] or '?'):10s} {(e['kind'] or '?'):16s}] "
            f"{((e['body'] or '')[:140]).replace(chr(10),' ')}"
            for e in ev
        )
        text = (
            f"{t['identifier']} · {t['status']} · {t['title']}\n"
            f"Created: {t['created']} · Updated: {t['updated']}\n\n"
            f"Body:\n{(t['body'] or '')[:600]}\n\n"
            f"Recent events:\n{ev_lines or '(none)'}"
        )
        return {"text": text, "score": 1.0, "source_uri": f"ticket:{identifier}"}
    except Exception:
        return None


def _mcp_call(tool: str, args: dict) -> Any:
    """Inline graph_rag MCP call via the existing sync helper."""
    from aiforge_core.runtime.api import _call_graph_mcp_sync  # type: ignore
    raw = _call_graph_mcp_sync(tool, args)
    if isinstance(raw, str) and raw.startswith("error:"):
        return None
    return raw


def _docs_lookup(library: str, text: str, *, top_k: int) -> list[dict]:
    from aiforge_core.index.docs_index import lookup_doc
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


def _tag(rows: list[dict], *, source: str, weight: float) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["source"] = d.get("source") or source
        d["score"] = float(d.get("score") or 0.5) * weight
        out.append(d)
    return out


_SYMBOL_HINT_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]+\b")


def _looks_like_symbol(text: str) -> bool:
    return bool(_SYMBOL_HINT_RE.search(text))


def _extract_symbol(text: str) -> str:
    m = _SYMBOL_HINT_RE.search(text)
    return m.group(0) if m else text[:40]


_LIBRARY_HINTS = {
    "spring":     ("spring", "springboot", "@autowired", "@restcontroller"),
    "react":      ("react ", "usestate", "useeffect", "jsx", "tsx"),
    "mongodb":    ("mongo", "aggregation", "$match", "$project"),
    "tekton":     ("tekton", "pipelinerun"),
    "kubernetes": ("kubectl", "deployment.yaml", "kubernetes", "k8s "),
}


def _guess_library(text: str) -> str | None:
    t = text.lower()
    for lib, hints in _LIBRARY_HINTS.items():
        if any(h in t for h in hints):
            return lib
    return None
