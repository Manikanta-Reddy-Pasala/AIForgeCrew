"""In-memory OKR DAG — built from the node folders, no database.

``build()`` parses every node and indexes the typed edges into fast lookups
(objective↔KR, KR→sessions, objective→learnings). Cheap to rebuild (flat files),
cached by the folder's newest mtime so a write is picked up automatically. The
``active_context`` pointer (which KR the agent is working on) persists in
``okr/.active.json`` so retrieval knows where to stand.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from . import nodes as _n
from . import store as _store


class Graph:
    def __init__(self, node_list: list[dict]):
        self.nodes: dict[str, dict] = {n["id"]: n for n in node_list if n.get("id")}
        self._objective_of: dict[str, str] = {}          # kr → objective
        self._krs: dict[str, list[str]] = defaultdict(list)      # objective → krs
        self._sessions: dict[str, list[str]] = defaultdict(list)  # kr → sessions
        self._scoped_learn: dict[str, list[str]] = defaultdict(list)  # o → learnings
        self._global_learn: list[str] = []
        for n in node_list:
            for kind, src, dst in _n.edges_of(n):
                if kind == "parent":
                    self._objective_of[src] = dst
                    self._krs[dst].append(src)
                elif kind == "covers":
                    self._sessions[dst].append(src)
                elif kind == "scopes":
                    self._scoped_learn[dst].append(src)
            if n.get("type") == "learning" and str(
                    (n.get("meta") or {}).get("scope")).lower() == "global":
                self._global_learn.append(n["id"])

    # ── traversal ──────────────────────────────────────────────────────
    def get(self, nid: str) -> dict | None:
        return self.nodes.get(nid)

    def objective_of(self, kr_id: str) -> str | None:
        return self._objective_of.get(kr_id)

    def key_results(self, objective_id: str) -> list[str]:
        return list(self._krs.get(objective_id, []))

    def sessions_of(self, kr_id: str, limit: int | None = None) -> list[str]:
        """Session ids covering a KR, NEWEST first (session ids are date-based,
        so a reverse string sort orders them chronologically)."""
        out = sorted(self._sessions.get(kr_id, []), reverse=True)
        return out[:limit] if limit else out

    def learnings_for(self, objective_id: str | None) -> list[str]:
        """Global learnings + those scoped to this objective (ids)."""
        return list(self._global_learn) + (
            list(self._scoped_learn.get(objective_id, [])) if objective_id else [])

    def counts(self) -> dict:
        by = defaultdict(int)
        for n in self.nodes.values():
            by[n.get("type") or "?"] += 1
        return dict(by)


# ── cached build (rebuild when the folder changes) ─────────────────────
_CACHE: dict = {"mtime": None, "graph": None}


def _folder_mtime() -> float:
    newest = 0.0
    root = _store.okr_root()
    for dp, _dn, fns in os.walk(root) if os.path.isdir(root) else []:
        for f in fns:
            if f.endswith(".md"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
                except OSError:
                    pass
    return newest


def build(*, force: bool = False) -> Graph:
    """The current DAG. Rebuilt only when a node file changed (or ``force``)."""
    mt = _folder_mtime()
    if not force and _CACHE["graph"] is not None and _CACHE["mtime"] == mt:
        return _CACHE["graph"]
    g = Graph(_store.load_all())
    _CACHE["mtime"], _CACHE["graph"] = mt, g
    return g


# ── active-context pointer ─────────────────────────────────────────────
def _active_path() -> str:
    return os.path.join(_store.okr_root(), ".active.json")


def set_active(kr_id: str | None) -> dict:
    """Mark the KR the agent is working on (None clears). Soft-fail."""
    os.makedirs(_store.okr_root(), exist_ok=True)
    try:
        with open(_active_path(), "w", encoding="utf-8") as fh:
            json.dump({"active_kr": kr_id or None}, fh)
        return {"ok": True, "active_kr": kr_id or None}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def get_active() -> str | None:
    try:
        with open(_active_path(), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("active_kr") or None
    except (OSError, ValueError):
        return None


__all__ = ["Graph", "build", "set_active", "get_active"]
