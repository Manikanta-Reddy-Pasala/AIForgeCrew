"""Shared helpers for the chat store — token/rank utilities + row normalizers.

Split out of the former single-file ``chat_store.py`` (grouped by concern:
helpers / sqlite backend / postgres backend / public API). No behaviour change.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime

_LOCK = threading.Lock()

# Very small stopword set — dropped so a query like "how do we handle X" keys
# off the meaningful terms (X) rather than matching every message with "how".
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "your", "our", "with",
    "how", "what", "why", "when", "who", "where", "should", "would", "could",
    "can", "does", "did", "this", "that", "these", "those", "from", "have",
    "has", "had", "will", "not", "but", "all", "any", "use", "used", "using",
})


# Inflection suffixes stripped to a STEM (longest match first) so a substring
# scan matches across word forms: query "deployment" → stem "deploy" matches
# "deployed"/"deploys"; "registries" → "registry". Cheap, no NLP dependency.
_STEM_SUFFIXES = ("ations", "ation", "ements", "ement", "ments", "ment",
                  "ings", "ing", "ies", "edly", "ers", "er", "ed", "es", "s")


def _stem(t: str) -> str:
    """Strip a common English inflection to a BARE stem (>=3 chars). Conservative:
    only for tokens len>=4, and the stem must stay >=3 so we don't over-crush
    short words into noise.

    The matcher is pure SUBSTRING, so the stem must be a substring of every
    inflected form it should unify. For ``ies`` we strip to the bare root
    (``registries -> registr``), NOT ``registry`` — ``registry`` is NOT a
    substring of ``registries`` (…tri… vs …try…), so the old ``ies -> y`` never
    matched the plural it was meant to. ``registr`` IS a substring of both
    ``registry`` and ``registries``, making the match symmetric."""
    if len(t) < 4:
        return t
    for suf in _STEM_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def _tokens(query: str) -> list[str]:
    """Lowercase alphanumeric tokens, len>=3, minus common stopwords — PLUS a
    stemmed variant of each so substring search matches across word forms
    (topic queries were failing on exact-form-only matching)."""
    if not query:
        return []
    raw = re.split(r"[^a-z0-9]+", query.lower())
    seen: dict[str, None] = {}
    for t in raw:
        if len(t) >= 3 and t not in _STOPWORDS:
            seen[t] = None
            st = _stem(t)
            if st != t and len(st) >= 3:
                seen[st] = None
    return list(seen)


def _iso(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# ── shared row normalizers (operate on plain dicts) ───────────────────────────

def _session_out(d: dict) -> dict:
    return {"id": d["id"], "title": d["title"], "cwd": d["cwd"],
            "role": (d.get("role") or "doer"),
            "created_at": _iso(d["created_at"]),
            "updated_at": _iso(d["updated_at"])}


def _message_out(d: dict) -> dict:
    try:
        steps = json.loads(d.get("steps") or "[]")
    except (ValueError, TypeError):
        steps = []
    return {"id": d["id"], "role": d["role"], "content": d["content"],
            "steps": steps, "checkpoint_sha": d.get("checkpoint_sha"),
            "mode": (d.get("mode") or "simple"),
            "duration_s": d.get("duration_s"),
            "created_at": _iso(d["created_at"])}


def _media_out(d: dict) -> dict:
    return {"id": d["id"], "session_id": d["session_id"],
            "filename": d["filename"], "path": d["path"],
            "mime": d["mime"], "description": d["description"],
            "created_at": _iso(d["created_at"])}


def _rank_search(rows: list[dict], toks: list[str], limit: int) -> list[dict]:
    """Shared ranking for search_messages — portable Python over candidate rows
    (keys: session_id, session_title, role, content, created_at, id)."""
    scored: list[tuple] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        low = content.lower()
        matched = sum(1 for t in toks if t in low)
        if matched == 0:
            continue
        # Density = total hits, so an on-topic message (a term repeated) ranks
        # above one incidental mention — breaking ties by relevance BEFORE
        # recency (id), not purely by how new the message is.
        density = sum(low.count(t) for t in toks)
        scored.append((matched, density, r["id"], r, content))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out: list[dict] = []
    for _matched, _density, _id, r, content in scored[:max(0, int(limit or 0))]:
        out.append({
            "session_id": r["session_id"],
            "session_title": r.get("session_title") or "Untitled chat",
            "role": r["role"],
            "content": content[:300],
            "created_at": _iso(r["created_at"]),
        })
    return out
