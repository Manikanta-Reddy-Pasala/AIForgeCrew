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
    "afm_bundle": 1.1,   # AiForgeMemory ContextBundle (chunks + repo_map +
                         # conventions + notes/docs + vector observations)
    "xrepo":      0.7,   # AiForgeMemory CALLS_REPO cross-repo edges
}


_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d+)\b")


def query(
    text: str, *,
    ticket: str | None = None,
    role: str | None = None,
    limit: int = 8,
    repo: str | None = None,
) -> dict:
    """Unified retrieval. Returns ``{hits, used_sources, errors}``.

    ``repo`` (optional) — repository name in the AiForgeMemory graph
    (Repo.name). When provided, enables source #7 (afm_bundle) which
    fetches code-graph context: chunks, repo_map, conventions,
    notes/docs, vector-recalled observations. Falls back to the
    ``AIFORGE_AFM_REPO`` env var when omitted.
    """
    if not text.strip():
        return {"hits": [], "used_sources": [], "errors": []}

    weights = _resolve_weights()
    used: list[str] = []
    errors: list[str] = []
    raw_hits: list[dict] = []

    # 1) Embedded SQLite recall — active only when no Neo4j/Postgres is
    # configured (backend_select.embedded()). Surfaces the agent's own
    # observations/failures/learnings written to ~/.aiforge/memory.db.
    # Soft-fail like every other source; the pro backends recall via
    # afm_bundle instead, so this stays dark when they're present.
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            sqlite_repo = repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None
            rows = _sqlmem.recall(text, limit=limit, repo=sqlite_repo)
            if rows:
                used.append("memory")
                raw_hits.extend(
                    _tag(rows, source="memory", weight=weights["memory"]),
                )
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

    # 3) related_memories — schema requires `key` (a repo/symbol/etc).
    # Use auto_ticket OR extracted symbol when available; otherwise
    # use the raw text.
    try:
        related_key = auto_ticket or (
            _extract_symbol(text) if _looks_like_symbol(text) else text
        )
        rows = _mcp_call("related_memories", {"key": related_key})
        if rows:
            used.append("related")
            raw_hits.extend(
                _tag(_unpack_mcp_rows(rows),
                     source="related", weight=weights["related"]),
            )
    except Exception as exc:
        errors.append(f"related: {exc}")

    # 4) sym_lookup — schema requires `query` (free-text).
    if _looks_like_symbol(text):
        try:
            rows = _mcp_call("sym_lookup", {
                "query": _extract_symbol(text),
                "k": min(limit, 10),
            })
            if rows:
                used.append("symbol")
                raw_hits.extend(
                    _tag(_unpack_mcp_rows(rows),
                         source="symbol", weight=weights["symbol"]),
                )
        except Exception as exc:
            errors.append(f"symbol: {exc}")

    # 5) find_doc — schema uses `k` not `top_k`.
    try:
        rows = _mcp_call("find_doc", {"query": text, "k": 3})
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

    # 7) AiForgeMemory ContextBundle — code-graph RAG (chunks/repo_map/
    # conventions/notes/docs/vector observations). Repo identifier from
    # caller kwarg or AIFORGE_AFM_REPO env fallback.
    afm_repo = repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None
    if afm_repo and os.environ.get("AIFORGE_AFM_BUNDLE_ENABLED", "1") == "1":
        try:
            rows = _afm_bundle(text, repo=afm_repo, role=role)
            if rows:
                used.append("afm_bundle")
                raw_hits.extend(
                    _tag(rows, source="afm_bundle",
                         weight=weights["afm_bundle"]),
                )
        except Exception as exc:
            errors.append(f"afm_bundle: {exc}")

    # 8) Cross-repo CALLS_REPO neighbours (gap A7). Retrieval normally
    # stops at the worktree boundary; surface "repo Y calls repo X" edges
    # AiForgeMemory already computes so a ticket touching X sees its
    # cross-repo callers/callees. Needs a known repo; flag-guarded.
    xrepo_repo = repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None
    if xrepo_repo and os.environ.get("AIFORGE_XREPO_ENABLED", "1") == "1":
        try:
            rows = _cross_repo_links(text, repo=xrepo_repo)
            if rows:
                used.append("xrepo")
                raw_hits.extend(
                    _tag(rows, source="xrepo", weight=weights["xrepo"]),
                )
        except Exception as exc:
            errors.append(f"xrepo: {exc}")

    raw_hits.sort(key=lambda h: -float(h.get("score") or 0))
    # Gap #3: diversify so one ticket / source can't flood the block.
    # Cap per group (ticket id if present, else source) — agentmemory's
    # session-diversification analog. Knob: AIFORGE_DIVERSIFY_PER_GROUP
    # (default 3, 0 disables).
    raw_hits = _diversify(raw_hits)
    # Optional cross-encoder rerank pass over the top-30 — biggest
    # quality jump per hour for natural-language → exact-symbol
    # retrieval. No-op when the reranker sidecar is not running.
    # Env knob: AIFORGE_RERANK_URL (default :8765 /rerank).
    try:
        reranked = _rerank_top(raw_hits[:30], query=text)
        if reranked:
            used.append("reranker")
            raw_hits = reranked + raw_hits[30:]
    except Exception as exc:
        errors.append(f"reranker: {exc}")

    return {
        "query": text,
        "hits": raw_hits[:limit],
        "used_sources": used,
        "errors": errors,
    }


def _diversify(hits: list[dict], *, per_group: int | None = None) -> list[dict]:
    """Cap how many hits any single origin contributes (gap #3).

    Group key = ``ticket`` when set, else ``source``. Walks ``hits`` in
    rank order keeping at most ``per_group`` per key; relative order is
    preserved. ``per_group <= 0`` disables (returns the input list).
    Default comes from ``AIFORGE_DIVERSIFY_PER_GROUP`` (3).
    """
    if per_group is None:
        try:
            per_group = int(os.environ.get("AIFORGE_DIVERSIFY_PER_GROUP", "3"))
        except ValueError:
            per_group = 3
    if per_group <= 0:
        return hits
    seen: dict[str, int] = {}
    out: list[dict] = []
    for h in hits:
        key = str(h.get("ticket") or h.get("group") or h.get("source") or "")
        n = seen.get(key, 0)
        if n >= per_group:
            continue
        seen[key] = n + 1
        out.append(h)
    return out


def _rerank_top(hits: list[dict], *, query: str) -> list[dict] | None:
    """POST hits to the reranker sidecar. Returns the same list with
    `rerank_score` field added and re-sorted desc. Returns None on any
    failure (caller falls back to unsorted list)."""
    if not hits or not query.strip():
        return None
    url = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")
    if not url:
        return None
    if os.environ.get("AIFORGE_RERANK_DISABLE", "0") == "1":
        return None
    try:
        import json as _json
        import urllib.request as _ur
        texts = [(h.get("text") or "")[:1500] for h in hits]
        body = _json.dumps({"query": query[:512], "texts": texts}).encode()
        req = _ur.Request(
            url.rstrip("/") + "/rerank",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=8) as r:
            resp = _json.loads(r.read())
        # Accept both shapes: list-of-{score} or {scores:[...]}
        if isinstance(resp, dict) and "scores" in resp:
            scores = resp["scores"]
        elif isinstance(resp, list):
            scores = [s.get("score") if isinstance(s, dict) else s
                      for s in resp]
        else:
            return None
        if len(scores) != len(hits):
            return None
        for h, s in zip(hits, scores):
            try:
                h["rerank_score"] = float(s)
                # Blend: 0.7 rerank + 0.3 original. Keeps source-weight
                # info (T2 fact > generic memory) while letting the
                # cross-encoder reorder near-ties.
                h["score"] = 0.7 * float(s) + 0.3 * float(h.get("score") or 0)
            except (TypeError, ValueError):
                continue
        hits.sort(key=lambda h: -float(h.get("score") or 0))
        return hits
    except Exception:
        return None


def render(result: dict) -> str:
    """Pretty render for prompt injection. KISS bullets."""
    if not result.get("hits"):
        return "[unified_memory] no hits"
    lines = ["[unified_memory] sources used: " + ", ".join(
        result.get("used_sources") or ["none"])]
    for i, h in enumerate(result["hits"], 1):
        src = h.get("source") or "?"
        text = (h.get("text") or "")[:300].replace("\n", " ")
        try:
            sc = float(h.get("score", 0) or 0)
        except (TypeError, ValueError):
            sc = 0.0
        lines.append(f"  {i}. [{src}|{sc:.2f}] {text}")
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
        from psycopg.rows import dict_row

        from aiforge_core.config.env import AIFORGE_DSN
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
    from aiforge_core.api.api import _call_graph_mcp_sync  # type: ignore
    raw = _call_graph_mcp_sync(tool, args)
    if isinstance(raw, str) and raw.startswith("error:"):
        return None
    return raw


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


def _afm_bundle(text: str, *, repo: str, role: str | None) -> list[dict]:
    """Fan-out into AiForgeMemory ContextBundle and flatten its sections
    into ranked rows. Section weights tuned so the most actionable bits
    (relevant chunks > repo_map > conventions > notes/docs > observations)
    surface first. Empty list on any backend failure.

    Sections produced:
    - repo_map        → 1 row, ranked-tag tree
    - conventions_md  → 1 row, .cursorrules
    - chunks          → up to 5 rows, one per anchor-file Chunk_v2
    - notes           → up to 3 rows, MENTIONS-linked Note_v2
    - docs            → up to 3 rows, MENTIONS-linked Doc_v2
    - observations    → up to 3 rows, vector-recalled Observation_v2
    """
    # Module renamed api/read.py → api/http.py in AiForgeMemory commit
    # 32d86ad (feature-vertical refactor). Support both so older installs
    # don't break the unified_query loop.
    try:
        from aiforge_memory.api.http import context_bundle_object
    except Exception:
        try:
            from aiforge_memory.api.read import context_bundle_object
        except Exception:
            return []
    role_arg = role or "doer"
    try:
        b = context_bundle_object(text, repo=repo, role=role_arg,
                                  token_budget=1000)
    except Exception:
        return []
    if b is None:
        return []

    out: list[dict] = []
    # Highest-signal first: repo_map (structure summary)
    if b.repo_map:
        out.append({
            "text": f"[afm/repo_map]\n{b.repo_map[:1500]}",
            "group": "afm:repo_map",
            "score": 0.95,
            "source_uri": f"afm://{repo}/repo_map",
        })
    # Conventions = project rules; second priority
    if b.conventions_md:
        out.append({
            "text": f"[afm/conventions]\n{b.conventions_md[:1500]}",
            "group": "afm:conventions",
            "score": 0.90,
            "source_uri": f"afm://{repo}/conventions",
        })
    # Code chunks — most concrete actionable evidence
    for c in (b.chunks or [])[:5]:
        path = c.get("file_path") or ""
        body = (c.get("text") or "").strip()
        if not path or not body:
            continue
        out.append({
            "text": f"[afm/chunk {path}]\n{body[:800]}",
            "group": "afm:chunk",
            "score": 0.85,
            "source_uri": f"afm://{repo}/{path}",
        })
    # Notes (MENTIONS-linked memos)
    for n in (b.notes or [])[:3]:
        title = n.get("title") or "Note"
        body = (n.get("body") or "").strip()
        if not body:
            continue
        out.append({
            "text": f"[afm/note {title}]\n{body[:600]}",
            "group": "afm:note",
            "score": 0.70,
            "source_uri": f"afm://{repo}/note/{n.get('id', '')}",
        })
    # External docs
    for d in (b.docs or [])[:3]:
        title = d.get("title") or "Doc"
        body = (d.get("body") or "").strip()
        url = d.get("url") or ""
        if not body:
            continue
        out.append({
            "text": f"[afm/doc {title}]\n{body[:600]}",
            "group": "afm:doc",
            "score": 0.65,
            "source_uri": url or f"afm://{repo}/doc/{d.get('id', '')}",
        })
    # Vector-recalled observations (typically agent learnings)
    for o in (b.observations or [])[:3]:
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


def _cross_repo_links(text: str, *, repo: str) -> list[dict]:
    """Surface AiForgeMemory CALLS_REPO edges touching ``repo`` (gap A7).

    Cross-repo retrieval: a ticket scoped to repo X normally can't see
    that repo Y calls into it. AiForgeMemory already computes these
    ``(Repo)-[:CALLS_REPO {via, confidence}]->(Repo)`` edges; we read
    them back via the link store and flatten to ranked rows so they
    participate in unified ranking.

    Reuses the same soft-fail import shim style as ``_afm_bundle`` —
    returns ``[]`` on any backend/import failure (never raises). The
    ``text`` arg is accepted for signature parity / future filtering but
    edges are repo-scoped, not text-scoped.

    Each row: ``{"text": "[xrepo] <from> --<via>--> <to> (conf <c>)",
    "score": <c>, "source": "xrepo"}``.
    """
    try:
        from aiforge_memory.api.commands._driver import driver
        from aiforge_memory.features.link.store import list_edges
    except Exception:
        return []
    try:
        drv = driver()
        edges = list_edges(drv, repo=repo) or []
    except Exception:
        return []

    out: list[dict] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("src") or "?"
        dst = e.get("dst") or "?"
        via = e.get("via") or "?"
        try:
            conf = float(e.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append({
            "text": f"[xrepo] {src} --{via}--> {dst} (conf {conf:.2f})",
            "score": conf,
            "source": "xrepo",
        })
    return out
