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


def _tokens(query: str) -> list[str]:
    """Lowercase alphanumeric tokens, len>=3, minus common stopwords."""
    if not query:
        return []
    raw = re.split(r"[^a-z0-9]+", query.lower())
    seen: dict[str, None] = {}
    for t in raw:
        if len(t) >= 3 and t not in _STOPWORDS:
            seen[t] = None
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
        scored.append((matched, r["id"], r, content))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out: list[dict] = []
    for _matched, _id, r, content in scored[:max(0, int(limit or 0))]:
        out.append({
            "session_id": r["session_id"],
            "session_title": r.get("session_title") or "Untitled chat",
            "role": r["role"],
            "content": content[:300],
            "created_at": _iso(r["created_at"]),
        })
    return out
