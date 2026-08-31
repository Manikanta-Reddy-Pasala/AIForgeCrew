"""Small utilities for unified_query: cache/TTL, per-source weights,
symbol/library detection, and the ``_tag`` row-decorator. Leaf module —
no cross-group imports so there are no import cycles."""
from __future__ import annotations

import os
import re

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
    "vector":     1.0,   # global (repo-agnostic) Observation_v2 vector/FT recall
    "chat":       0.6,   # prior chat-session message content (chat_store)
    "keyword":    0.9,   # BM25 keyword/exact-id recall (FTS5), fused with vector
    "recent":     0.7,   # hot cache: most-recently-written units (fresh facts)
}


_TICKET_RE = re.compile(r"\b([A-Z]{2,5}-\d+)\b")


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


def _tag(rows: list[dict], *, source: str, weight: float) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["source"] = d.get("source") or source
        # The retrieval CHANNEL (which index answered), kept SEPARATE from the
        # display ``source`` (which is the unit's stored origin, e.g.
        # ``compacted:<stem>``). Lets the UI/API split semantic-vector hits from
        # md-file/keyword hits even when a brief carries its own source string.
        d["channel"] = source
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


_SYMBOL_HINT_RE = re.compile(r"\b[A-Z]\w+\b")


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
