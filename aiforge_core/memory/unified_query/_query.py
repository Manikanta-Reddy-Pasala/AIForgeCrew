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
from ._query_sources import (  # noqa: F401  # re-exported
    _graphify_rows,
    _RecallCtx,
    _recent_min_overlap,
    _relevant_recent,
    _src_chat,
    _src_doc,
    _src_external,
    _src_global_vector,
    _src_graphify,
    _src_keyword,
    _src_recent,
    _src_related,
    _src_sqlite_recall,
    _src_symbol,
    _src_ticket,
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


def _pinned_constraints(ctx: _RecallCtx) -> list[dict]:
    """STANDING RULES for this repo, pinned ahead of the ranked hits.

    Deliberately NOT one of ``_RECALL_SOURCES``: a constraint must not be
    ranked. Going through the fuser would put it back in competition with
    cosine hits, where "never push straight to production" loses to six kafka
    notes on a kafka question — and a rule that is only injected when the
    question happens to share vocabulary with it is a rule the agent breaks.
    Added after ranking, so it also survives the limit.

    Bounded by AIFORGE_UMEM_CONSTRAINTS_N (default 8) so a store full of rules
    can't crowd out retrieval; off with AIFORGE_UMEM_CONSTRAINTS=0. Soft-fails.
    """
    if os.environ.get("AIFORGE_UMEM_CONSTRAINTS", "1") != "1":
        return []
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return []
        from aiforge_core.memory import sqlite_memory as _sqlmem
        try:
            cn = max(1, int(os.environ.get("AIFORGE_UMEM_CONSTRAINTS_N", "8")))
        except (TypeError, ValueError):
            cn = 8
        rows = _sqlmem.constraints(repo=ctx._repo_or_env(), limit=cn)
        if not rows:
            return []
        ctx.used.append("constraint")
        return [{**r, "channel": "constraint", "pinned": True,
                 "_raw_score": 1.0, "_weight": 1.0} for r in rows]
    except Exception as exc:  # noqa: BLE001 — rules must never break recall
        ctx.errors.append(f"constraint: {exc}")
        return []


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


def _apply_pinned_rules(ctx: _RecallCtx, top: list, ranked: list) -> tuple:
    """Put the repo's standing rules at the HEAD of both hit lists.

    Applied AFTER ranking and the limit: a rule is an obligation, not a result,
    and a truncated obligation is a broken one.

    The de-dup runs the OTHER way round from the usual: a rule that also
    happened to match the query is still a rule, so the RANKED copy goes and
    the pinned one stays. Dropping the pinned copy instead cost the rule its
    framing — it came back as an 0.08-scoring search result with no obligation
    attached, and the pipeline's mandatory-RULES heading vanished with it.
    """
    rules = _pinned_constraints(ctx)
    if not rules:
        return top, ranked
    pinned_txt = {(r.get("text") or "").strip() for r in rules}

    def _not_pinned(h) -> bool:
        return (h.get("text") or "").strip() not in pinned_txt

    return (rules + [h for h in top if _not_pinned(h)],
            rules + [h for h in ranked if _not_pinned(h)])


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
    top, ranked_predupe = _apply_pinned_rules(ctx, top, ranked_predupe)

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
        if h.get("pinned"):
            # A rule reads as a rule, not as the top search result.
            lines.append(f"  {i}. [RULE] {text}")
            continue
        try:
            sc = float(h.get("score", 0) or 0)
        except (TypeError, ValueError):
            sc = 0.0
        lines.append(f"  {i}. [{src}|{sc:.2f}] {text}")
    if result.get("errors"):
        lines.append("[errors] " + "; ".join(result["errors"]))
    return "\n".join(lines)
