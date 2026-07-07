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

Ranking: each source emits a ``score`` (roughly [0, 1]); we min-max
normalise it per source (so each source's top hit maps to its weight
ceiling — comparable across sources), multiply by the per-source weight
(tunable via ``AIFORGE_UMEM_WEIGHT_<source>``), then dedup identical
content across sources and take top-K. Normalisation is monotonic within
a source, so within-source order is unchanged; disable via
``AIFORGE_UMEM_NORMALIZE=0`` to restore the legacy weight-scaled scores.

Public surface:
- ``query(text, *, ticket=None, role=None, limit=8) -> dict``
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any

# Short-TTL result cache — the pull runs on every chat/pipeline turn (+ some
# turns query more than once). Identical (text, repo, role, limit, session)
# recalls within the TTL skip the ~10 backend round-trips. Tunable /
# disable-able via AIFORGE_UMEM_CACHE_TTL (seconds; 0 = off).
_QCACHE: dict = {}
_QCACHE_MAX = 256


def _qcache_ttl() -> float:
    try:
        return float(os.environ.get("AIFORGE_UMEM_CACHE_TTL", "45"))
    except ValueError:
        return 45.0

_DEFAULT_WEIGHTS = {
    "memory":     1.0,
    "ticket":     1.2,
    "related":    0.8,
    "symbol":     0.9,
    "graphify":   0.85,  # graphify concept-graph neighbours (graph.json)
    "doc":        0.6,
    "external":   0.5,
    "afm_bundle": 1.1,   # AiForgeMemory ContextBundle (chunks + repo_map +
                         # conventions + notes/docs + vector observations)
    "vector":     1.0,   # global (repo-agnostic) Observation_v2 vector/FT recall
    "xrepo":      0.7,   # AiForgeMemory CALLS_REPO cross-repo edges
    "chat":       0.6,   # prior chat-session message content (chat_store)
}


_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d+)\b")


def query(
    text: str, *,
    ticket: str | None = None,
    role: str | None = None,
    limit: int = 8,
    repo: str | None = None,
    exclude_session: int | None = None,
    session_id: int | None = None,
    boost_tags: list[str] | None = None,
) -> dict:
    """Unified retrieval. Returns ``{hits, used_sources, errors}``.

    ``repo`` (optional) — repository name in the AiForgeMemory graph
    (Repo.name). When provided, enables source #7 (afm_bundle) which
    fetches code-graph context: chunks, repo_map, conventions,
    notes/docs, vector-recalled observations. Falls back to the
    ``AIFORGE_AFM_REPO`` env var when omitted.

    ``exclude_session`` / ``session_id`` (optional, aliases) — the CURRENT
    chat session id. Threaded into the chat source so proactive recall during
    a live turn does not surface the ongoing conversation as "prior chat"
    (gap M4). ``session_id`` is accepted as a friendly alias.
    """
    exclude_session = exclude_session if exclude_session is not None else session_id
    if not text.strip():
        return {"hits": [], "used_sources": [], "errors": []}

    _ck = (text.strip().lower(), repo or "", role or "", int(limit),
           exclude_session, tuple(sorted(boost_tags or ())))
    _ttl = _qcache_ttl()
    if _ttl > 0:
        _hit = _QCACHE.get(_ck)
        if _hit is not None and (time.time() - _hit[0]) < _ttl:
            return _hit[1]

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
            rows = _sqlmem.recall(text, limit=limit, repo=sqlite_repo,
                                  boost_tags=boost_tags)
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
                    "_raw_score": 1.0,
                    "_weight": weights["ticket"],
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

    # 4b) graphify concept graph — nodes + neighbours related to the query
    # (label / source path / substring), read from graphify-out/graph.json
    # (repo_root resolved from AIFORGE_REPO_ROOT). Soft-fails when the repo has
    # no graph. Pulls the code-concept structure into recall automatically
    # instead of relying on the agent to call the graphify_lookup tool.
    try:
        from aiforge_core.runtime.graphify_lookup_tool import graphify_lookup
        gr = graphify_lookup(text, hops=1, max_neighbors=12)
        if gr.get("ok"):
            grows: list[dict] = []
            for m in (gr.get("matches") or [])[:6]:
                sf = m.get("source_file") or ""
                grows.append({"text": f"{m.get('label', '')}{' — ' + sf if sf else ''}",
                              "score": 0.8, "id": m.get("id")})
            for n in (gr.get("neighbors") or [])[:12]:
                nd = n.get("node")
                label = nd.get("label") if isinstance(nd, dict) else str(nd or "")
                grows.append({"text": f"{label} ({n.get('relation', 'related')})",
                              "score": float(n.get("weight") or 0.5)})
            grows = [g for g in grows if g["text"].strip()]
            if grows:
                used.append("graphify")
                raw_hits.extend(
                    _tag(grows, source="graphify", weight=weights["graphify"]))
    except Exception as exc:  # noqa: BLE001 — soft-fail like every other source
        errors.append(f"graphify: {exc}")

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

    # 7b) Global (repo-agnostic) Observation_v2 vector + fulltext recall.
    # The AFM bundle above only fires with a scoped repo, so a repo-less
    # GLOBAL search (the Memory UI's "search across all wings", freshly
    # ingested repos before any repo is pinned) never saw ingested code/doc
    # observations. Query the vector + fulltext indexes directly across all
    # repos. Neo4j-only (embedded SQLite recall is source 1); soft-fail.
    # Contamination guard: this source is repo-AGNOSTIC (queries Observation_v2
    # across ALL repos). For a SCOPED task (repo given) that bleeds an unrelated
    # task's context into the plan — the "game leaked into tempconv" bug. Only
    # run it for a repo-less GLOBAL search (Memory UI). Opt back in for a scoped
    # task with AIFORGE_UMEM_CROSS_TASK=1.
    _cross_task = os.environ.get("AIFORGE_UMEM_CROSS_TASK", "0") == "1"
    if (repo is None or _cross_task) \
            and os.environ.get("AIFORGE_UMEM_GLOBAL_VECTOR", "1") == "1":
        try:
            from aiforge_core.memory import backend_select as _bsel
            if not _bsel.embedded():
                rows = _global_vector_recall(text, limit=limit)
                if rows:
                    used.append("vector")
                    raw_hits.extend(
                        _tag(rows, source="vector", weight=weights["vector"]),
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"vector: {exc}")

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

    # 9) Prior chat-session content (gap F3). Chat messages live in their
    # own chat_store SQLite silo the team pipeline never read, so what was
    # worked out in chat was invisible to ticket runs (only distilled facts
    # bridged). Surface it as a low-weight source so it informs without
    # dominating. Gated by AIFORGE_UMEM_CHAT (default on); soft-fails to [].
    # Contamination guard (same as 7b): _chat_sessions searches ALL prior chat
    # messages by text only — no repo filter — so generic build phrasing ("build
    # a Python library with pytest tests") matches an UNRELATED past task and
    # drags its content in. That was the concrete cross-task leak. For a scoped
    # task skip it unless AIFORGE_UMEM_CROSS_TASK=1; keep it for global search.
    if (repo is None or _cross_task) \
            and os.environ.get("AIFORGE_UMEM_CHAT", "1") == "1":
        try:
            rows = _chat_sessions(text, limit=limit,
                                  exclude_session=exclude_session)
            if rows:
                used.append("chat")
                raw_hits.extend(
                    _tag(rows, source="chat", weight=weights["chat"]),
                )
        except Exception as exc:
            errors.append(f"chat: {exc}")

    # Pre-rank fix: min-max normalize each source's scores to [0,1] before
    # the weight applies, so a fixed-score source (ticket 1.0, afm 0.95…)
    # can't auto-bury a real cosine-relevance hit. Soft-fail → un-normalized.
    try:
        raw_hits = _normalize_scores(raw_hits)
    except Exception as exc:  # noqa: BLE001 — ranking must never break query
        errors.append(f"normalize: {exc}")

    raw_hits.sort(key=lambda h: -float(h.get("score") or 0))

    # Cross-source content dedup: the same doc can arrive from find_doc AND
    # afm_bundle; keep the highest-scored copy. Soft-fail → un-deduped.
    try:
        raw_hits = _dedup(raw_hits)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"dedup: {exc}")

    # Gap #3: diversify so one ticket / source can't flood the block.
    # Cap per group (ticket id if present, else source) — agentmemory's
    # session-diversification analog. Knob: AIFORGE_DIVERSIFY_PER_GROUP
    # (default 3, 0 disables). Single-source recall (embedded SQLite) is
    # exempt from the cap so it can't be squashed to 3 (see _diversify).
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

    result = {
        "query": text,
        "hits": raw_hits[:limit],
        "used_sources": used,
        "errors": errors,
    }
    if _ttl > 0:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()   # simple bound — cheap, TTL keeps it fresh anyway
        _QCACHE[_ck] = (time.time(), result)
    return result


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

    def _key(h: dict) -> str:
        return str(h.get("ticket") or h.get("group") or h.get("source") or "")

    # Single-source case: when every hit collapses to ONE group (e.g. the
    # embedded SQLite backend where recall rows all share source="doer"),
    # capping would drop a limit=8 recall down to 3 real hits. Skip the cap
    # and let the caller's [:limit] slice bound the result instead.
    distinct = {_key(h) for h in hits}
    if len(distinct) <= 1:
        return hits

    seen: dict[str, int] = {}
    out: list[dict] = []
    for h in hits:
        key = _key(h)
        n = seen.get(key, 0)
        if n >= per_group:
            continue
        seen[key] = n + 1
        out.append(h)
    return out


def _dedup(hits: list[dict]) -> list[dict]:
    """Drop duplicate content arriving from multiple sources, keeping the
    highest-scored copy. Key priority:

    1. ``source_uri`` when present — the original intent was cross-source
       SAME-doc dedup (the same doc arriving via find_doc AND afm_bundle).
    2. else a FULL-text SHA1 hash of the normalized (strip+lower) body — so
       two DISTINCT facts that merely share a long boilerplate PREFIX are
       NOT collapsed (a 200-char-prefix key silently dropped recall).
    3. else object identity (distinct empty-text hits never merge).

    Relative order follows the first appearance of each key; on a real
    collision the highest weighted ``score`` wins. Extra keys survive."""
    def _score(h: dict) -> float:
        try:
            return float(h.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    order: list[object] = []
    best: dict[object, dict] = {}
    for h in hits:
        uri = h.get("source_uri")
        if uri:
            key: object = ("uri", str(uri))
        else:
            text = (h.get("text") or "").strip().lower()
            if text:
                key = ("txt", hashlib.sha1(text.encode("utf-8")).hexdigest())
            else:
                key = ("id", id(h))
        if key not in best:
            best[key] = h
            order.append(key)
        elif _score(h) > _score(best[key]):
            best[key] = h
    return [best[k] for k in order]


def _abs_weight() -> float:
    """Blend factor between ABSOLUTE raw relevance and per-source min-max
    normalization (``AIFORGE_UMEM_ABS_WEIGHT``, default 0.5). 1.0 = pure raw
    (normalization off), 0.0 = pure min-max (legacy). Clamped to [0,1]."""
    try:
        v = float(os.environ.get("AIFORGE_UMEM_ABS_WEIGHT", "0.5"))
    except (TypeError, ValueError):
        v = 0.5
    return min(1.0, max(0.0, v))


def _normalize_scores(hits: list[dict]) -> list[dict]:
    """Rank-fair per-source scaling that PRESERVES an absolute relevance floor.

    Pure min-max normalization has two failure modes (both reproduced by the
    adversarial audit):
      * a source with ONE hit (or an all-equal band) maps to norm 1.0 → a
        marginal raw=0.20 singleton ``doc`` becomes ``1.0×weight`` and
        outranks strong raw=0.80+ ``memory`` facts;
      * the lowest member of a TIGHT strong band (0.80-0.85) is driven to
        norm 0.0 and sinks below a weak singleton.

    Fix: blend the min-max norm with the clamped raw cosine so absolute
    relevance keeps mattering:
        ``final = weight × (ABS_W × raw + (1-ABS_W) × norm)``
    and for the ``span<=0`` single-hit / all-equal case use the RAW score
    (clamped) × weight — NOT 1.0 — so a weak singleton stays weak.

    Uses ``_raw_score`` / ``_weight`` stashed by :func:`_tag` (falls back to
    the existing ``score``). Monotonic within a source → within-source order
    preserved. Gated by ``AIFORGE_UMEM_NORMALIZE`` (default on; 0/false keeps
    the legacy weight-scaled ``score`` untouched)."""
    if not hits:
        return hits
    if os.environ.get("AIFORGE_UMEM_NORMALIZE", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return hits

    abs_w = _abs_weight()
    groups: dict[str, list[dict]] = {}
    for h in hits:
        groups.setdefault(str(h.get("source") or ""), []).append(h)

    for group in groups.values():
        raws = [_raw_of(h) for h in group]
        lo, hi = min(raws), max(raws)
        span = hi - lo
        for h in group:
            w = float(h.get("_weight", 1.0))
            raw_c = min(1.0, max(0.0, _raw_of(h)))
            if span <= 0:
                # Single hit / all-equal band: keep ABSOLUTE relevance — a
                # weak singleton must stay weak (was norm 1.0 = auto-top).
                h["score"] = raw_c * w
            else:
                norm = (_raw_of(h) - lo) / span
                h["score"] = (abs_w * raw_c + (1.0 - abs_w) * norm) * w
    return hits


def _raw_of(h: dict) -> float:
    try:
        return float(h.get("_raw_score", h.get("score") or 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


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

        from aiforge_core.net.ssl import context_for as _ssl_context_for
        texts = [(h.get("text") or "")[:1500] for h in hits]
        body = _json.dumps({"query": query[:512], "texts": texts}).encode()
        rerank_url = url.rstrip("/") + "/rerank"
        req = _ur.Request(
            rerank_url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=8, context=_ssl_context_for(rerank_url)) as r:
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
    except Exception:
        return None
    # Only the DB round-trip is treated as "ticket unavailable" → None. The
    # result-shaping below is deliberately OUTSIDE this narrow except: a renamed
    # column / key typo (KeyError) must SURFACE (it propagates to the caller,
    # which records it in the unified-query ``errors`` list) instead of being
    # silently indistinguishable from a real DB outage.
    try:
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
    except (psycopg.Error, OSError):
        return None
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


def _mcp_call(tool: str, args: dict) -> Any:
    """Inline graph_rag MCP call via the existing sync helper."""
    from aiforge_core.api.api import _call_mcp_sync  # type: ignore
    raw = _call_mcp_sync(tool, args)
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


def _global_vector_recall(text: str, *, limit: int) -> list[dict]:
    """Repo-agnostic recall over ``Observation_v2`` on the Neo4j backend.

    The AFM ContextBundle (source 7) only surfaces vector-recalled observations
    when a ``repo`` is scoped. A GLOBAL memory search (the /api/memory/search UI,
    ingested repos, cross-wing "search everything") passes no repo, so those
    observations were invisible. This queries the ``codemem_observation_embed``
    vector index directly (semantic) and unions the ``codemem_observation_ft``
    fulltext index (lexical) across ALL repos. Soft-fail → []."""
    from aiforge_core.runtime.memory_ingest import _neo4j_driver_or_none
    drv = _neo4j_driver_or_none()
    if drv is None:
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        with drv.session() as s:
            try:
                from aiforge_core.memory.embed import embed as _embed
                qv = _embed(text)
            except Exception:  # noqa: BLE001 — embed sidecar down → FT only
                qv = None
            if qv:
                for r in s.run(
                    "CALL db.index.vector.queryNodes"
                    "('codemem_observation_embed', $k, $v) "
                    "YIELD node, score "
                    "RETURN node.id AS id, node.text AS text, node.kind AS kind, "
                    "node.repo AS repo, score AS score",
                    k=min(limit, 20), v=qv,
                ).data():
                    if r.get("id") and r["id"] not in seen and r.get("text"):
                        seen.add(r["id"])
                        rows.append({"text": r["text"], "score": float(r.get("score") or 0.5),
                                     "kind": r.get("kind"), "repo": r.get("repo")})
            # Lexical union (catches exact tokens a paraphrased vector misses).
            try:
                for r in s.run(
                    "CALL db.index.fulltext.queryNodes"
                    "('codemem_observation_ft', $q) "
                    "YIELD node, score "
                    "RETURN node.id AS id, node.text AS text, node.kind AS kind, "
                    "node.repo AS repo, score AS score LIMIT $k",
                    q=text, k=min(limit, 20),
                ).data():
                    if r.get("id") and r["id"] not in seen and r.get("text"):
                        seen.add(r["id"])
                        rows.append({"text": r["text"], "score": float(r.get("score") or 0.5),
                                     "kind": r.get("kind"), "repo": r.get("repo")})
            except Exception:  # noqa: BLE001 — FT query syntax on odd input
                pass
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
    return rows


def _tag(rows: list[dict], *, source: str, weight: float) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["source"] = d.get("source") or source
        raw = float(d.get("score") or 0.5)
        # Keep the pre-weight raw score + weight so _normalize_scores can
        # min-max rescale per source before ranking (fixed-score sources
        # otherwise auto-outrank real cosine hits). ``score`` stays as the
        # provisional weight-scaled value for backward compat / soft-fail.
        d["_raw_score"] = raw
        d["_weight"] = weight
        d["score"] = raw * weight
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
            # Per-chunk group (not a shared "afm:chunk") so _diversify's
            # per-group cap doesn't drop 5 doer-evidence chunks down to 3.
            "text": f"[afm/chunk {path}]\n{body[:800]}",
            "group": f"afm:chunk:{path}",
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
