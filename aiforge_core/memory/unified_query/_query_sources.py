"""The recall context and the sources a query fans out to: vector, keyword,
recent, ticket, related, symbol, graph, documents, external, global and chat."""
from __future__ import annotations

import os

from ._helpers import _TICKET_RE


class _RecallCtx:
    """Shared state threaded through the retrieval sources. ``pkg`` is the
    package object used for late-bound helper resolution (so a test that
    monkeypatches ``unified_query.<helper>`` is honoured); ``used`` / ``errors``
    / ``raw_hits`` are the accumulators each source appends to."""

    def __init__(self, *, text, ticket, role, limit, repo, exclude_session,
                 boost_tags, weights, pkg, cross_project=False):
        self.text = text
        self.ticket = ticket
        self.role = role
        self.limit = limit
        self.repo = repo
        self.exclude_session = exclude_session
        self.boost_tags = boost_tags
        self.weights = weights
        self.pkg = pkg
        # A CHAT recall ranks its own project first and may also be offered
        # memory from outside it. Off for tickets and the pipeline, where a
        # scoped task must not see another task's memory.
        self.cross_project = bool(cross_project)
        self.used: list[str] = []
        self.errors: list[str] = []
        self.raw_hits: list[dict] = []
        # `(search(...) or [None])[0]` worked — a Match supports [0] — but it
        # reads as an index into a list that may be empty, which is how an
        # analyser reads it too. Say which one it is.
        _m = _TICKET_RE.search(text)
        self.auto_ticket = ticket or (_m.group(0) if _m else None)
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


def _recent_min_overlap() -> float:
    """How much of the question a fresh row must actually mention to be offered
    (``AIFORGE_UMEM_RECENT_MIN_OVERLAP``, default 0.2; 0 = ungated)."""
    try:
        return max(0.0, float(
            os.environ.get("AIFORGE_UMEM_RECENT_MIN_OVERLAP", "0.2")))
    except (TypeError, ValueError):
        return 0.2


def _relevant_recent(rows: list, text: str) -> list:
    """Recent rows that are ABOUT the query, scored by how well they match.

    ``recent()`` scores by position — the newest row gets 1.0 — which with the
    channel weight made the last thing written score 0.7 on EVERY query,
    above a genuine cosine hit. Recency is a tie-breaker, not relevance: scale
    each row's recency score by its overlap with the question and drop the rows
    that say nothing about it, so the hot cache can surface a just-captured
    fact without displacing a real match."""
    from ._helpers import _lexical_overlap
    floor = _recent_min_overlap()
    out = []
    for r in rows:
        overlap = _lexical_overlap(text, f"{r.get('title') or ''} {r.get('text') or ''}")
        if overlap < floor or overlap <= 0.0:
            continue
        out.append({**r, "score": float(r.get("score") or 0.0) * overlap})
    return out


def _src_recent(ctx: "_RecallCtx") -> None:
    """1c) HOT CACHE — the N most-recently-written units (fresh facts that may
    not be embedded/compacted yet), so a just-captured learning surfaces
    immediately. Query-gated (see :func:`_relevant_recent`). Embedded backend
    only; gated by AIFORGE_UMEM_RECENT."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded() and os.environ.get("AIFORGE_UMEM_RECENT", "1") == "1":
            from aiforge_core.memory import sqlite_memory as _sqlmem
            try:
                rn = max(1, int(os.environ.get("AIFORGE_UMEM_RECENT_N", "5")))
            except (TypeError, ValueError):
                rn = 5
            rrows = _relevant_recent(
                _sqlmem.recent(limit=rn, repo=ctx._repo_or_env()), ctx.text)
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
    # `gr` comes off a tool result, so neither key is guaranteed to be a list —
    # take the sequence explicitly instead of slicing whatever arrived.
    matches = gr.get("matches")
    neighbors = gr.get("neighbors")
    for m in (list(matches)[:6] if isinstance(matches, (list, tuple)) else []):
        sf = m.get("source_file") or ""
        grows.append({"text": f"{m.get('label', '')}{' — ' + sf if sf else ''}",
                      "score": 0.8, "id": m.get("id")})
    for n in (list(neighbors)[:12]
              if isinstance(neighbors, (list, tuple)) else []):
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


_GLOBAL_REPOS = ("", "shared")
# Buckets that are not a project and are kept out of scoped recall on purpose:
# hand-dropped notes and per-session summaries ("notes"), and the rule books
# ("rules"). Prior chats are already reached by the chat source.
_NOT_A_PROJECT = ("notes", "rules")


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _general_key() -> str:
    from aiforge_core.memory.projects import GENERAL
    return GENERAL


def _cross_project_on(ctx: "_RecallCtx") -> bool:
    return (ctx.cross_project and bool(ctx._repo_or_env())
            and os.environ.get("AIFORGE_UMEM_CROSS_PROJECT", "1") == "1")


def _src_cross_project(ctx: "_RecallCtx") -> None:
    """1d) Memory from OUTSIDE the chat's own scope — other projects, and the
    general bucket chats without a project write to.

    The scoped sources above only see this project plus global. This one looks
    at everything else, and is deliberately hard to get through: a row must
    clear a relevance floor (``AIFORGE_UMEM_CROSS_MIN_SCORE``) and at most
    ``AIFORGE_UMEM_CROSS_MAX`` are offered, at a lower weight, so an unrelated
    project can never crowd out the chat's own memory. A row from another
    project carries the ``project`` it came from so the prompt can say so."""
    if not _cross_project_on(ctx):
        return
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return
        from aiforge_core.memory import sqlite_memory as _sqlmem
        own = ctx._repo_or_env()
        floor = _float_env("AIFORGE_UMEM_CROSS_MIN_SCORE", 0.45)
        cap = max(0, int(_float_env("AIFORGE_UMEM_CROSS_MAX", 2)))
        if cap <= 0:
            return
        rows = _sqlmem.recall(ctx.text, limit=max(ctx.limit * 3, 12), repo=None,
                              boost_tags=ctx.boost_tags)
        keep = []
        for r in rows:
            rp = (r.get("repo") or "").strip()
            if rp in _GLOBAL_REPOS or rp == own or rp in _NOT_A_PROJECT:
                continue
            if float(r.get("score") or 0.0) < floor:
                continue
            # The shared no-project bucket is "outside" but is not a project
            # to name in the prompt.
            keep.append({**r, **({} if rp == _general_key() else {"project": rp})})
            if len(keep) >= cap:
                break
        if keep:
            ctx.used.append("cross")
            ctx.raw_hits.extend(ctx.pkg._tag(keep, source="cross",
                                             weight=ctx.weights["cross"]))
    except Exception as exc:  # noqa: BLE001
        ctx.errors.append(f"cross: {exc}")


def _src_chat(ctx: "_RecallCtx") -> None:
    """9) Prior chat-session content (gap F3). Chat messages live in their own
    chat_store silo the pipeline never read. Surface as a low-weight source so it
    informs without dominating. ON for scoped calls too (default) — disable with
    AIFORGE_UMEM_CHAT_SCOPED=0 (or AIFORGE_UMEM_CHAT=0 entirely).

    A recall that has a project reads only THAT project's chats. What another
    chat did in another repo ("Fixed, 1 passed", the files it edited) is that
    repo's outcome: recalled here it reads as this repo's, and the agent goes
    looking for those files. Durable knowledge from elsewhere still arrives
    through the memory sources, which carry a scope. A repo-less search, and a
    recall with AIFORGE_UMEM_CROSS_TASK=1, read every chat as before."""
    chat_scoped_ok = (ctx.repo is None or ctx.cross_task
                      or os.environ.get("AIFORGE_UMEM_CHAT_SCOPED", "1") == "1")
    if not (chat_scoped_ok and os.environ.get("AIFORGE_UMEM_CHAT", "1") == "1"):
        return
    try:
        project = None if ctx.cross_task else ctx._repo_or_env()
        rows = ctx.pkg._chat_sessions(
            ctx.text, limit=ctx.limit, exclude_session=ctx.exclude_session,
            **({"project": project} if project else {}))
        if rows:
            ctx.used.append("chat")
            ctx.raw_hits.extend(ctx.pkg._tag(rows, source="chat",
                                             weight=ctx.weights["chat"]))
    except Exception as exc:
        ctx.errors.append(f"chat: {exc}")
