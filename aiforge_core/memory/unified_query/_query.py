"""The unified ``query`` entry point (fan-out + rank + merge) and the
``render`` prompt-formatter. Layers on top of the leaf submodules
(``_helpers`` / ``_ranking`` / ``_sources``).

Helper FUNCTIONS are resolved through the package object (``_pkg``) at call
time rather than bound as locals, so that monkeypatching
``unified_query.<helper>`` on the package is honoured by ``query`` exactly as
it was when everything lived in one module. Constants (``_QCACHE`` singleton,
``_TICKET_RE``) are imported directly — the cache is mutated on the same object
either way, and nothing patches the regex.
"""
from __future__ import annotations

import os
import sys
import time

from ._helpers import (
    _QCACHE,
    _QCACHE_MAX,
    _TICKET_RE,
    _extract_symbol,  # noqa: F401 — fallback namespace for _pkg resolution
    _guess_library,  # noqa: F401
    _looks_like_symbol,  # noqa: F401
    _qcache_ttl,  # noqa: F401
    _resolve_weights,  # noqa: F401
    _tag,  # noqa: F401
)
from ._ranking import (
    _dedup,  # noqa: F401
    _diversify,  # noqa: F401
    _normalize_scores,  # noqa: F401
    _rerank_top,  # noqa: F401
)
from ._sources import (
    _chat_sessions,  # noqa: F401
    _docs_lookup,  # noqa: F401
    _global_vector_recall,  # noqa: F401
    _mcp_call,  # noqa: F401
    _ticket_brief,  # noqa: F401
    _unpack_mcp_rows,  # noqa: F401
)


class _RecallCtx:
    """Shared state threaded through the retrieval sources. ``pkg`` is the
    package object used for late-bound helper resolution (so a test that
    monkeypatches ``unified_query.<helper>`` is honoured); ``used`` / ``errors``
    / ``raw_hits`` are the accumulators each source appends to."""

    def __init__(self, *, text, ticket, role, limit, repo, exclude_session,
                 boost_tags, weights, pkg):
        self.text = text
        self.ticket = ticket
        self.role = role
        self.limit = limit
        self.repo = repo
        self.exclude_session = exclude_session
        self.boost_tags = boost_tags
        self.weights = weights
        self.pkg = pkg
        self.used: list[str] = []
        self.errors: list[str] = []
        self.raw_hits: list[dict] = []
        self.auto_ticket = ticket or (_TICKET_RE.search(text) or [None])[0]
        self.cross_task = os.environ.get("AIFORGE_UMEM_CROSS_TASK", "0") == "1"

    def _repo_or_env(self):
        return self.repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None


def _src_sqlite_recall(ctx: "_RecallCtx") -> None:
    """1) Embedded SQLite vector recall. Surfaces the agent's own
    observations/failures/learnings written to ~/.aiforge/memory.db."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            rows = _sqlmem.recall(ctx.text, limit=ctx.limit, repo=ctx._repo_or_env(),
                                  boost_tags=ctx.boost_tags)
            if rows:
                ctx.used.append("memory")
                ctx.raw_hits.extend(ctx.pkg._tag(rows, source="memory",
                                                 weight=ctx.weights["memory"]))
    except Exception as exc:
        ctx.errors.append(f"memory: {exc}")


def _src_keyword(ctx: "_RecallCtx") -> None:
    """1b) KEYWORD/BM25 recall (FTS5) — hybrid partner to the vector 'memory'
    source. Catches exact ids / service names / hashes that embeddings blur, with
    spell correction. Fused by the same per-source normalize+weight."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            krows = _sqlmem.keyword_search(ctx.text, repo=ctx._repo_or_env(),
                                           limit=ctx.limit)
            if krows:
                ctx.used.append("keyword")
                ctx.raw_hits.extend(ctx.pkg._tag(krows, source="keyword",
                                                 weight=ctx.weights["keyword"]))
    except Exception as exc:  # noqa: BLE001
        ctx.errors.append(f"keyword: {exc}")


def _src_recent(ctx: "_RecallCtx") -> None:
    """1c) HOT CACHE — the N most-recently-written units (fresh facts that may
    not be embedded/compacted yet), so a just-captured learning surfaces
    immediately. Embedded backend only; gated by AIFORGE_UMEM_RECENT."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded() and os.environ.get("AIFORGE_UMEM_RECENT", "1") == "1":
            from aiforge_core.memory import sqlite_memory as _sqlmem
            try:
                rn = max(1, int(os.environ.get("AIFORGE_UMEM_RECENT_N", "5")))
            except (TypeError, ValueError):
                rn = 5
            rrows = _sqlmem.recent(limit=rn, repo=ctx._repo_or_env())
            if rrows:
                ctx.used.append("recent")
                ctx.raw_hits.extend(ctx.pkg._tag(rrows, source="recent",
                                                 weight=ctx.weights["recent"]))
    except Exception as exc:  # noqa: BLE001
        ctx.errors.append(f"recent: {exc}")


def _src_ticket(ctx: "_RecallCtx") -> None:
    """2) Ticket brief — explicit ticket OR auto-detected token."""
    if not ctx.auto_ticket:
        return
    try:
        row = ctx.pkg._ticket_brief(ctx.auto_ticket)
        if row:
            ctx.used.append("ticket")
            w = ctx.weights["ticket"]
            ctx.raw_hits.append({**row, "source": "ticket", "channel": "ticket",
                                 "_raw_score": 1.0, "_weight": w, "score": 1.0 * w})
    except Exception as exc:
        ctx.errors.append(f"ticket: {exc}")


def _src_related(ctx: "_RecallCtx") -> None:
    """3) related_memories — schema requires `key` (a repo/symbol/etc). Use
    auto_ticket OR extracted symbol when available; otherwise the raw text."""
    try:
        related_key = ctx.auto_ticket or (
            ctx.pkg._extract_symbol(ctx.text)
            if ctx.pkg._looks_like_symbol(ctx.text) else ctx.text)
        rows = ctx.pkg._mcp_call("related_memories", {"key": related_key})
        if rows:
            ctx.used.append("related")
            ctx.raw_hits.extend(ctx.pkg._tag(ctx.pkg._unpack_mcp_rows(rows),
                                             source="related",
                                             weight=ctx.weights["related"]))
    except Exception as exc:
        ctx.errors.append(f"related: {exc}")


def _src_symbol(ctx: "_RecallCtx") -> None:
    """4) sym_lookup — schema requires `query` (free-text)."""
    if not ctx.pkg._looks_like_symbol(ctx.text):
        return
    try:
        rows = ctx.pkg._mcp_call("sym_lookup", {
            "query": ctx.pkg._extract_symbol(ctx.text), "k": min(ctx.limit, 10)})
        if rows:
            ctx.used.append("symbol")
            ctx.raw_hits.extend(ctx.pkg._tag(ctx.pkg._unpack_mcp_rows(rows),
                                             source="symbol",
                                             weight=ctx.weights["symbol"]))
    except Exception as exc:
        ctx.errors.append(f"symbol: {exc}")


def _graphify_rows(gr: dict) -> "list[dict]":
    """Flatten a graphify_lookup result into scored recall rows (top matches +
    neighbours), dropping empties."""
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
    return [g for g in grows if g["text"].strip()]


def _src_graphify(ctx: "_RecallCtx") -> None:
    """4b) graphify concept graph — nodes + neighbours related to the query,
    read from graphify-out/graph.json. Pulls code-concept structure into recall
    automatically instead of relying on the agent to call the tool."""
    try:
        from aiforge_core.runtime.graphify_lookup_tool import graphify_lookup
        gr = graphify_lookup(ctx.text, hops=1, max_neighbors=12)
        if gr.get("ok"):
            grows = _graphify_rows(gr)
            if grows:
                ctx.used.append("graphify")
                ctx.raw_hits.extend(ctx.pkg._tag(grows, source="graphify",
                                                 weight=ctx.weights["graphify"]))
    except Exception as exc:  # noqa: BLE001 — soft-fail like every other source
        ctx.errors.append(f"graphify: {exc}")


def _src_doc(ctx: "_RecallCtx") -> None:
    """5) find_doc — schema uses `k` not `top_k`."""
    try:
        rows = ctx.pkg._mcp_call("find_doc", {"query": ctx.text, "k": 3})
        if rows:
            ctx.used.append("doc")
            ctx.raw_hits.extend(ctx.pkg._tag(ctx.pkg._unpack_mcp_rows(rows),
                                             source="doc", weight=ctx.weights["doc"]))
    except Exception as exc:
        ctx.errors.append(f"doc: {exc}")


def _src_external(ctx: "_RecallCtx") -> None:
    """6) external docs (library guessed from query)."""
    library = ctx.pkg._guess_library(ctx.text)
    if not library:
        return
    try:
        rows = ctx.pkg._docs_lookup(library, ctx.text, top_k=2)
        if rows:
            ctx.used.append(f"external:{library}")
            ctx.raw_hits.extend(ctx.pkg._tag(rows, source=f"external:{library}",
                                             weight=ctx.weights["external"]))
    except Exception as exc:
        ctx.errors.append(f"external:{library}: {exc}")


def _src_global_vector(ctx: "_RecallCtx") -> None:
    """7b) Global (repo-agnostic) Observation_v2 vector + fulltext recall.

    The AFM bundle only fires with a scoped repo, so a repo-less GLOBAL search
    never saw ingested code/doc observations. Contamination guard: a SCOPED task
    runs a REPO-SCOPED vector recall (no cross-task bleed — the "game leaked into
    tempconv" bug); repo-less calls stay global; cross-repo bleed for a scoped
    task needs AIFORGE_UMEM_CROSS_TASK=1. Soft-fail."""
    if os.environ.get("AIFORGE_UMEM_GLOBAL_VECTOR", "1") != "1":
        return
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            vrepo = None if (ctx.repo is None or ctx.cross_task) else ctx.repo
            rows = ctx.pkg._global_vector_recall(ctx.text, limit=ctx.limit, repo=vrepo)
            if rows:
                ctx.used.append("vector")
                ctx.raw_hits.extend(ctx.pkg._tag(rows, source="vector",
                                                 weight=ctx.weights["vector"]))
    except Exception as exc:  # noqa: BLE001
        ctx.errors.append(f"vector: {exc}")


def _src_chat(ctx: "_RecallCtx") -> None:
    """9) Prior chat-session content (gap F3). Chat messages live in their own
    chat_store silo the pipeline never read. Surface as a low-weight source so it
    informs without dominating. ON for scoped calls too (default) — disable with
    AIFORGE_UMEM_CHAT_SCOPED=0 (or AIFORGE_UMEM_CHAT=0 entirely)."""
    chat_scoped_ok = (ctx.repo is None or ctx.cross_task
                      or os.environ.get("AIFORGE_UMEM_CHAT_SCOPED", "1") == "1")
    if not (chat_scoped_ok and os.environ.get("AIFORGE_UMEM_CHAT", "1") == "1"):
        return
    try:
        rows = ctx.pkg._chat_sessions(ctx.text, limit=ctx.limit,
                                      exclude_session=ctx.exclude_session)
        if rows:
            ctx.used.append("chat")
            ctx.raw_hits.extend(ctx.pkg._tag(rows, source="chat",
                                             weight=ctx.weights["chat"]))
    except Exception as exc:
        ctx.errors.append(f"chat: {exc}")


_RECALL_SOURCES = (
    _src_sqlite_recall, _src_keyword, _src_recent, _src_ticket, _src_related,
    _src_symbol, _src_graphify, _src_doc, _src_external,
    _src_global_vector, _src_chat,
)


def _fuse_and_rank(ctx: "_RecallCtx") -> "tuple[list[dict], list[dict]]":
    """Normalize per-source scores, sort, dedup, diversify and rerank the raw
    hits. Returns ``(top, ranked_predupe)`` — ``top`` is the final limited list,
    ``ranked_predupe`` the pre cross-channel-dedup ranked view for the UI split.

    Pre-rank fix: min-max normalize each source's scores to [0,1] before the
    weight applies, so a fixed-score source (ticket 1.0…) can't auto-bury a real
    cosine-relevance hit. Every stage soft-fails."""
    pkg, errors = ctx.pkg, ctx.errors
    hits = ctx.raw_hits
    try:
        hits = pkg._normalize_scores(hits)
    except Exception as exc:  # noqa: BLE001 — ranking must never break query
        errors.append(f"normalize: {exc}")
    hits.sort(key=lambda h: -float(h.get("score") or 0))
    # Snapshot the ranked hits BEFORE cross-channel dedup so the API can show
    # each channel's OWN results (the flat list collapses a brief that matched
    # BOTH the vector KNN and the keyword index into one copy).
    ranked_predupe = list(hits)
    try:
        hits = pkg._dedup(hits)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"dedup: {exc}")
    # Gap #3: diversify so one ticket / source can't flood the block.
    hits = pkg._diversify(hits)
    try:
        reranked = pkg._rerank_top(hits[:30], query=ctx.text)
        if reranked:
            ctx.used.append("reranker")
            hits = reranked + hits[30:]
    except Exception as exc:
        errors.append(f"reranker: {exc}")
    return hits[:ctx.limit], ranked_predupe


def _linked_additions(ctx: "_RecallCtx", top: list[dict]) -> "list[dict]":
    """LINK EXPANSION: follow each matched brief's Links section (wired by
    map_scopes) and return the connected briefs' FULL knowledge text so a hit
    surfaces its neighbours too. Embedded (md-brief) backend only; soft-fails;
    gated by AIFORGE_UMEM_LINK_EXPAND."""
    if os.environ.get("AIFORGE_UMEM_LINK_EXPAND", "1") != "1":
        return []
    try:
        from aiforge_core.memory import md_store as _mds
        srcs = [h.get("source") for h in top if h.get("source")]
        if not srcs:
            return []
        linked = _mds.expand_links(srcs, max_links=max(3, ctx.limit // 2))
        seen_txt = {(h.get("text") or "").strip() for h in top}
        add: list[dict] = []
        for lk in linked:
            body = (lk.get("text") or "").strip()
            if not body or body in seen_txt:
                continue
            seen_txt.add(body)
            add.append({"text": body, "source": lk.get("source"),
                        "channel": "linked", "kind": lk.get("kind"),
                        "title": lk.get("title"), "score": 0.0, "linked": True,
                        "source_uri": f"linked://{lk.get('file')}"})
        if add:
            ctx.used.append("linked")
        return add
    except Exception as exc:  # noqa: BLE001 — expansion must never break query
        ctx.errors.append(f"linked: {exc}")
        return []


def _mirror_recall_to_langfuse(text: str, repo, used: list, result: dict,
                               errors: list) -> None:
    """Make MEMORY RECALL observable next to the LLM calls it feeds — what was
    asked, which sources answered, what came back. Env-gated, fire-and-forget;
    soft-fails, recall never breaks."""
    try:
        from aiforge_core.integrations import langfuse_adapter as _lf
        if not _lf.enabled():
            return
        summary = "\n".join(
            f"[{h.get('source') or h.get('source_uri') or '?'}] "
            + str(h.get('text') or '')[:200] for h in result["hits"][:8])
        _lf.record_generation(
            role="memory.recall", model=",".join(used) or "none",
            messages=[{"role": "user", "content": text[:2000]}],
            output=summary,
            metadata={"path": "memory", "sources": used,
                      "hits": len(result["hits"]),
                      **({"errors": errors[:3]} if errors else {}),
                      **({"repo": repo} if repo else {})})
    except Exception:  # noqa: BLE001 — tracing must never break recall
        pass


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

    ``repo`` (optional) — repository scope for the recall sources that take
    one. Falls back to the ``AIFORGE_AFM_REPO`` env var when omitted. (The
    Neo4j-backed AiForgeMemory bundle and cross-repo sources that used to key
    off this were removed with the graph layer — this build is SQLite-only.)

    ``exclude_session`` / ``session_id`` (optional, aliases) — the CURRENT chat
    session id. Threaded into the chat source so proactive recall during a live
    turn does not surface the ongoing conversation as "prior chat" (gap M4).
    """
    # Resolve helper functions through the CURRENT package object (not a
    # module-level cache) so monkeypatching ``unified_query.<helper>`` is
    # honoured — including when a test's fixture pops+re-imports the package.
    # When the package has been popped from sys.modules (a test popped it but
    # still holds a live reference to this fn), fall back to THIS submodule's own
    # namespace, which carries the same helper names as unpatched defaults.
    _pkg = sys.modules.get(__package__) or sys.modules[__name__]
    exclude_session = exclude_session if exclude_session is not None else session_id
    if not text.strip():
        return {"hits": [], "used_sources": [], "errors": []}

    _ck = (text.strip().lower(), repo or "", role or "", int(limit),
           exclude_session, tuple(sorted(boost_tags or ())))
    _ttl = _pkg._qcache_ttl()
    if _ttl > 0:
        _hit = _QCACHE.get(_ck)
        if _hit is not None and (time.time() - _hit[0]) < _ttl:
            return _hit[1]

    ctx = _RecallCtx(text=text, ticket=ticket, role=role, limit=limit, repo=repo,
                     exclude_session=exclude_session, boost_tags=boost_tags,
                     weights=_pkg._resolve_weights(), pkg=_pkg)
    for source in _RECALL_SOURCES:
        source(ctx)

    top, ranked_predupe = _fuse_and_rank(ctx)
    add = _linked_additions(ctx, top)
    if add:
        top = top + add
        ranked_predupe = ranked_predupe + add

    result = {
        "query": text,
        "hits": top,
        # Per-channel ranked view (pre cross-channel dedup) for the UI/API split.
        "ranked": ranked_predupe,
        "used_sources": ctx.used,
        "errors": ctx.errors,
    }
    if _ttl > 0:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()   # simple bound — cheap, TTL keeps it fresh anyway
        _QCACHE[_ck] = (time.time(), result)
    _mirror_recall_to_langfuse(text, repo, ctx.used, result, ctx.errors)
    return result


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
