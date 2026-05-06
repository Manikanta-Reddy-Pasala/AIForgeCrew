"""Active mid-task memory recall — the Doer calls this when it hits
an unknown class/method/concept it can't find by reading nearby files.

Wraps :mod:`aiforge_core.memory.unified_query` (vector + fulltext +
1-hop graph, fused via RRF) into a tool-friendly shape that returns a
JSON-serialisable dict on success and a clean ``ok=False`` on backend
failure — never raises into the agent loop.
"""
from __future__ import annotations


def memory_lookup(query: str, k: int = 6) -> dict:
    """Search AiForgeMemory for ``query``.

    Args:
      query: free-form text — symbol, error message, natural question.
      k: max hits to return (clamped to ``[1, 12]``; default 6).

    Returns:
      ``{ok, hits: [{source, score, body}], used_sources: [...]}`` on
      success. ``{ok: False, error}`` when the memory backend is
      unreachable or the query layer raised.
    """
    try:
        from aiforge_core.memory import unified_query as _uq
    except Exception as exc:
        return {"ok": False, "error": f"memory backend missing: {exc}"}

    capped_k = max(1, min(12, int(k or 6)))
    try:
        result = _uq.query(query, limit=capped_k)
    except Exception as exc:
        return {"ok": False, "error": f"memory query failed: {exc}"}

    hits: list[dict] = []
    for h in (result.get("hits") or [])[:capped_k]:
        body = (h.get("text") or h.get("body") or h.get("summary") or "")[:600]
        hits.append({
            "source": h.get("source", "?"),
            "score": float(h.get("score", 0.0) or 0.0),
            "body": body,
        })
    return {
        "ok": True,
        "hits": hits,
        "used_sources": result.get("used_sources") or [],
    }


__all__ = ["memory_lookup"]
