"""Ranking / merging pass for unified_query hits: per-source min-max
normalization (with an absolute-relevance floor), cross-source content
dedup, per-origin diversification, and the optional cross-encoder rerank.
Leaf module — pure functions over hit dicts, no cross-group imports."""
from __future__ import annotations

import hashlib
import os


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
                key = ("txt", hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest())
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


def _fetch_rerank_scores(url: str, query: str, hits: list[dict]) -> "list | None":
    """POST hits to the reranker sidecar and return its per-hit scores, or None
    on any failure / unexpected shape (accepts ``{scores:[...]}`` or a list of
    ``{score}``)."""
    import json as _json
    import urllib.request as _ur
    from aiforge_core.net.ssl import context_for as _ssl_context_for
    texts = [(h.get("text") or "")[:1500] for h in hits]
    body = _json.dumps({"query": query[:512], "texts": texts}).encode()
    rerank_url = url.rstrip("/") + "/rerank"
    req = _ur.Request(rerank_url, data=body,
                      headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=8, context=_ssl_context_for(rerank_url)) as r:
        resp = _json.loads(r.read())
    if isinstance(resp, dict) and "scores" in resp:
        return resp["scores"]
    if isinstance(resp, list):
        return [s.get("score") if isinstance(s, dict) else s for s in resp]
    return None


def _apply_rerank_scores(hits: list[dict], scores: list) -> None:
    """Blend each hit's rerank score into its score: 0.7 rerank + 0.3 original,
    which keeps source-weight info (T2 fact > generic memory) while letting the
    cross-encoder reorder near-ties. Then sort desc."""
    for h, s in zip(hits, scores):
        try:
            h["rerank_score"] = float(s)
            h["score"] = 0.7 * float(s) + 0.3 * float(h.get("score") or 0)
        except (TypeError, ValueError):
            continue
    hits.sort(key=lambda h: -float(h.get("score") or 0))


def _rerank_top(hits: list[dict], *, query: str) -> list[dict] | None:
    """POST hits to the reranker sidecar. Returns the same list with
    `rerank_score` added and re-sorted desc. Returns None on any failure (caller
    falls back to the unsorted list)."""
    if not hits or not query.strip():
        return None
    url = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")
    if not url or os.environ.get("AIFORGE_RERANK_DISABLE", "0") == "1":
        return None
    try:
        scores = _fetch_rerank_scores(url, query, hits)
        if scores is None or len(scores) != len(hits):
            return None
        _apply_rerank_scores(hits, scores)
        return hits
    except Exception:
        return None
