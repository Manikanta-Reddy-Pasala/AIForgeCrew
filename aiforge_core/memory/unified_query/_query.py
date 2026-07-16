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
    _afm_bundle,  # noqa: F401
    _chat_sessions,  # noqa: F401
    _cross_repo_links,  # noqa: F401
    _docs_lookup,  # noqa: F401
    _global_vector_recall,  # noqa: F401
    _mcp_call,  # noqa: F401
    _ticket_brief,  # noqa: F401
    _unpack_mcp_rows,  # noqa: F401
)


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
    # Resolve helper functions through the CURRENT package object (not a
    # module-level cache) so monkeypatching ``unified_query.<helper>`` is
    # honoured — including when a test's fixture pops+re-imports the package
    # (submodules are not re-executed, so a cached ref would go stale). When
    # the package has been popped from sys.modules (a test popped it but still
    # holds a live reference to this query fn), fall back to THIS submodule's
    # own namespace — which carries the same helper names as unpatched
    # defaults, mirroring how the original single-file module's globals kept
    # working after it was removed from sys.modules.
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

    weights = _pkg._resolve_weights()
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
                    _pkg._tag(rows, source="memory", weight=weights["memory"]),
                )
    except Exception as exc:
        errors.append(f"memory: {exc}")

    # 1b) KEYWORD/BM25 recall (FTS5) — hybrid partner to the vector 'memory'
    # source. Catches exact ids / service names / hashes that embeddings blur,
    # with spell correction. Fused by the same per-source normalize+weight below.
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            _krepo = repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None
            krows = _sqlmem.keyword_search(text, repo=_krepo, limit=limit)
            if krows:
                used.append("keyword")
                raw_hits.extend(
                    _pkg._tag(krows, source="keyword", weight=weights["keyword"]))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"keyword: {exc}")

    # 1c) HOT CACHE — the N most-recently-written units (fresh facts that may not
    # be embedded/compacted yet). A small always-on source so a just-captured
    # learning surfaces immediately. Embedded backend only; gated by
    # AIFORGE_UMEM_RECENT (default on).
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded() and os.environ.get("AIFORGE_UMEM_RECENT", "1") == "1":
            from aiforge_core.memory import sqlite_memory as _sqlmem
            _rrepo = repo or os.environ.get("AIFORGE_AFM_REPO", "").strip() or None
            try:
                _rn = max(1, int(os.environ.get("AIFORGE_UMEM_RECENT_N", "5")))
            except (TypeError, ValueError):
                _rn = 5
            rrows = _sqlmem.recent(limit=_rn, repo=_rrepo)
            if rrows:
                used.append("recent")
                raw_hits.extend(
                    _pkg._tag(rrows, source="recent", weight=weights["recent"]))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"recent: {exc}")

    # 2) Ticket brief — explicit ticket OR auto-detected token
    auto_ticket = ticket or (_TICKET_RE.search(text) or [None])[0]
    if auto_ticket:
        try:
            row = _pkg._ticket_brief(auto_ticket)
            if row:
                used.append("ticket")
                raw_hits.append({
                    **row, "source": "ticket", "channel": "ticket",
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
            _pkg._extract_symbol(text) if _pkg._looks_like_symbol(text) else text
        )
        rows = _pkg._mcp_call("related_memories", {"key": related_key})
        if rows:
            used.append("related")
            raw_hits.extend(
                _pkg._tag(_pkg._unpack_mcp_rows(rows),
                          source="related", weight=weights["related"]),
            )
    except Exception as exc:
        errors.append(f"related: {exc}")

    # 4) sym_lookup — schema requires `query` (free-text).
    if _pkg._looks_like_symbol(text):
        try:
            rows = _pkg._mcp_call("sym_lookup", {
                "query": _pkg._extract_symbol(text),
                "k": min(limit, 10),
            })
            if rows:
                used.append("symbol")
                raw_hits.extend(
                    _pkg._tag(_pkg._unpack_mcp_rows(rows),
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
                    _pkg._tag(grows, source="graphify", weight=weights["graphify"]))
    except Exception as exc:  # noqa: BLE001 — soft-fail like every other source
        errors.append(f"graphify: {exc}")

    # 5) find_doc — schema uses `k` not `top_k`.
    try:
        rows = _pkg._mcp_call("find_doc", {"query": text, "k": 3})
        if rows:
            used.append("doc")
            raw_hits.extend(
                _pkg._tag(_pkg._unpack_mcp_rows(rows),
                          source="doc", weight=weights["doc"]),
            )
    except Exception as exc:
        errors.append(f"doc: {exc}")

    # 6) external docs (library guessed from query)
    library = _pkg._guess_library(text)
    if library:
        try:
            rows = _pkg._docs_lookup(library, text, top_k=2)
            if rows:
                used.append(f"external:{library}")
                raw_hits.extend(
                    _pkg._tag(rows,
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
            rows = _pkg._afm_bundle(text, repo=afm_repo, role=role)
            if rows:
                used.append("afm_bundle")
                raw_hits.extend(
                    _pkg._tag(rows, source="afm_bundle",
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
    # When a repo is scoped we now run a REPO-SCOPED vector recall (filtered to
    # that repo — no cross-task bleed), so a scoped chat/pipeline turn actually
    # gets its own repo's learnings/observations (the whole point). Repo-less
    # calls stay global. Cross-repo bleed for a scoped task still needs the
    # explicit AIFORGE_UMEM_CROSS_TASK=1 opt-in.
    _cross_task = os.environ.get("AIFORGE_UMEM_CROSS_TASK", "0") == "1"
    if os.environ.get("AIFORGE_UMEM_GLOBAL_VECTOR", "1") == "1":
        try:
            from aiforge_core.memory import backend_select as _bsel
            if not _bsel.embedded():
                # scoped → filter to repo; repo-less OR cross-task opt-in → global
                _vrepo = None if (repo is None or _cross_task) else repo
                rows = _pkg._global_vector_recall(text, limit=limit, repo=_vrepo)
                if rows:
                    used.append("vector")
                    raw_hits.extend(
                        _pkg._tag(rows, source="vector", weight=weights["vector"]),
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
            rows = _pkg._cross_repo_links(text, repo=xrepo_repo)
            if rows:
                used.append("xrepo")
                raw_hits.extend(
                    _pkg._tag(rows, source="xrepo", weight=weights["xrepo"]),
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
            rows = _pkg._chat_sessions(text, limit=limit,
                                       exclude_session=exclude_session)
            if rows:
                used.append("chat")
                raw_hits.extend(
                    _pkg._tag(rows, source="chat", weight=weights["chat"]),
                )
        except Exception as exc:
            errors.append(f"chat: {exc}")

    # Pre-rank fix: min-max normalize each source's scores to [0,1] before
    # the weight applies, so a fixed-score source (ticket 1.0, afm 0.95…)
    # can't auto-bury a real cosine-relevance hit. Soft-fail → un-normalized.
    try:
        raw_hits = _pkg._normalize_scores(raw_hits)
    except Exception as exc:  # noqa: BLE001 — ranking must never break query
        errors.append(f"normalize: {exc}")

    raw_hits.sort(key=lambda h: -float(h.get("score") or 0))

    # Snapshot the ranked hits BEFORE cross-channel dedup. The flat `hits` below
    # collapse a brief that matched BOTH the vector KNN and the keyword index
    # into one copy (right for agents), but that hides the vector index behind
    # the keyword copy in the UI's per-origin split. Keep the pre-dedup ranked
    # list so the API can show each channel's OWN results (overlap expected).
    ranked_predupe = list(raw_hits)

    # Cross-source content dedup: the same doc can arrive from find_doc AND
    # afm_bundle; keep the highest-scored copy. Soft-fail → un-deduped.
    try:
        raw_hits = _pkg._dedup(raw_hits)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"dedup: {exc}")

    # Gap #3: diversify so one ticket / source can't flood the block.
    # Cap per group (ticket id if present, else source) — agentmemory's
    # session-diversification analog. Knob: AIFORGE_DIVERSIFY_PER_GROUP
    # (default 3, 0 disables). Single-source recall (embedded SQLite) is
    # exempt from the cap so it can't be squashed to 3 (see _diversify).
    raw_hits = _pkg._diversify(raw_hits)
    # Optional cross-encoder rerank pass over the top-30 — biggest
    # quality jump per hour for natural-language → exact-symbol
    # retrieval. No-op when the reranker sidecar is not running.
    # Env knob: AIFORGE_RERANK_URL (default :8765 /rerank).
    try:
        reranked = _pkg._rerank_top(raw_hits[:30], query=text)
        if reranked:
            used.append("reranker")
            raw_hits = reranked + raw_hits[30:]
    except Exception as exc:
        errors.append(f"reranker: {exc}")

    top = raw_hits[:limit]

    # LINK EXPANSION: search returns the briefs that MATCHED; each brief's Links
    # section (wired by map_scopes) points at its load-bearing neighbours. Follow
    # those edges and append the connected briefs' FULL knowledge text so a hit
    # surfaces its related briefs too — "search goes through the links and gives
    # full info". Embedded (md-brief) backend only; soft-fails; gated by
    # AIFORGE_UMEM_LINK_EXPAND (default on).
    if os.environ.get("AIFORGE_UMEM_LINK_EXPAND", "1") == "1":
        try:
            from aiforge_core.memory import md_store as _mds
            srcs = [h.get("source") for h in top if h.get("source")]
            if srcs:
                linked = _mds.expand_links(srcs, max_links=max(3, limit // 2))
                seen_txt = {(h.get("text") or "").strip() for h in top}
                add: list[dict] = []
                for lk in linked:
                    body = (lk.get("text") or "").strip()
                    if not body or body in seen_txt:
                        continue
                    seen_txt.add(body)
                    add.append({
                        "text": body, "source": lk.get("source"),
                        "channel": "linked",
                        "kind": lk.get("kind"), "title": lk.get("title"),
                        "score": 0.0, "linked": True,
                        "source_uri": f"linked://{lk.get('file')}",
                    })
                if add:
                    used.append("linked")
                    top = top + add
                    ranked_predupe = ranked_predupe + add
        except Exception as exc:  # noqa: BLE001 — expansion must never break query
            errors.append(f"linked: {exc}")

    result = {
        "query": text,
        "hits": top,
        # Per-channel ranked view (pre cross-channel dedup) for the UI/API split
        # so the vector index shows its OWN results instead of only the briefs
        # that survived dedup against the keyword copy.
        "ranked": ranked_predupe,
        "used_sources": used,
        "errors": errors,
    }
    if _ttl > 0:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()   # simple bound — cheap, TTL keeps it fresh anyway
        _QCACHE[_ck] = (time.time(), result)
    # Langfuse mirror (env-gated, fire-and-forget): make MEMORY RECALL
    # observable next to the LLM calls it feeds — what was asked, which
    # sources answered, what came back. Soft-fails; recall never breaks.
    try:
        from aiforge_core.integrations import langfuse_adapter as _lf
        if _lf.enabled():
            _summary = "\n".join(
                f"[{h.get('source') or h.get('source_uri') or '?'}] "
                + str(h.get('text') or '')[:200]
                for h in result["hits"][:8])
            _lf.record_generation(
                role="memory.recall", model=",".join(used) or "none",
                messages=[{"role": "user", "content": text[:2000]}],
                output=_summary,
                metadata={"path": "memory", "sources": used,
                          "hits": len(result["hits"]),
                          **({"errors": errors[:3]} if errors else {}),
                          **({"repo": repo} if repo else {})})
    except Exception:  # noqa: BLE001 — tracing must never break recall
        pass
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
