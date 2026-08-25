"""Memory admin — cross-store overview + per-datasource destructive clear.

Gives the operator a high-level, blank-slate-killing view of the memory stores
the app writes to, plus an idempotent "empty this store" action per datasource.
Two hard rules:

  * **Data only, never config.** A clear wipes indexed DATA (embeddings,
    SQLite units, on-disk notes, chat history). It NEVER deletes the user's
    registered memory *sources* (repos/dirs) or any settings — those survive
    so the user can simply re-index to repopulate. After a full wipe each
    source's index state is reset to idle/units=0.

  * The graph datasources (``graph_facts`` / ``symbols`` / ``graphify`` /
    ``chunks``) were backed by an OPTIONAL graph backend that has been removed
    — this is a SQLite-only build. They are always reported as unavailable so
    the UI keeps rendering their (empty) tiles without special-casing.

Every store soft-fails independently: one unreachable store never breaks the
whole overview / clear.
"""
from __future__ import annotations

from aiforge_core.memory import backend_select

# Preserved after any clear — surfaced in every clear result so the UI can
# reassure the operator their registered repos/settings are intact.
_PRESERVE_NOTE = "config + source registrations preserved — re-index to repopulate"

# Graph datasources — previously backed by an optional graph store, now removed.
# Surfaced in the overview as permanently-unavailable so the UI still renders
# their tiles (identical to how a SQLite deployment always behaved).
_GRAPH_STORES = ("graph_facts", "symbols", "graphify", "chunks")
_GRAPH_REMOVED_REASON = "graph backend removed (SQLite-only build)"

# Datasources a clear can target. NOTE: 'sources' is deliberately absent —
# registrations are config, not data, and must survive a wipe.
CLEARABLE = ["graph_facts", "symbols", "graphify", "chunks",
             "sqlite", "md_files", "chat"]


# ─────────────────────────────── overview ───────────────────────────────────

def _graph_sections() -> dict:
    """The (removed) graph datasources — always unavailable."""
    return {k: {"available": False, "reason": _GRAPH_REMOVED_REASON}
            for k in _GRAPH_STORES}


def _sqlite_section() -> dict:
    from aiforge_core.memory import sqlite_memory
    s = sqlite_memory.stats()
    return {"total": s.get("total", 0), "by_kind": s.get("by_kind", {}),
            "path": s.get("db_path")}


def _md_section() -> dict:
    from aiforge_core.memory import md_store
    d = md_store.memory_dir()
    files = list(d.glob("*.md"))
    total = sum(p.stat().st_size for p in files if p.is_file())
    return {"count": len(files), "bytes": int(total), "dir": str(d)}


def _chat_section() -> dict:
    from aiforge_core.runtime import chat_store
    sessions = chat_store.list_sessions()
    messages = sum(int(s.get("message_count") or 0) for s in sessions)
    return {"sessions": len(sessions), "messages": messages}


def _sources_section() -> dict:
    """VIEW ONLY — the registered repos/dirs + their status. Never cleared."""
    from aiforge_core.runtime import memory_sources
    items = memory_sources.list_sources()
    return {"count": len(items),
            "by_status": memory_sources.status_counts(),
            "items": items}


def _safe(fn) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}


def memory_overview() -> dict:
    """Per-store high-level breakdown across every memory datasource.

    Shape::

        {"backend": "sqlite"|"postgres",
         "stores": {
            "graph_facts": {"available": False, "reason": ".."},  # removed
            "symbols":     {"available": False, "reason": ".."},  # removed
            "graphify":    {"available": False, "reason": ".."},  # removed
            "chunks":      {"available": False, "reason": ".."},  # removed
            "sqlite":      {"total": n, "by_kind": {..}, "path": ".."},
            "md_files":    {"count": n, "bytes": n, "dir": ".."},
            "chat":        {"sessions": n, "messages": n},
            "sources":     {"count": n, "by_status": {..}, "items": [..]},  # VIEW ONLY
         }}

    Every store soft-fails independently — one down store never breaks the rest.
    """
    stores: dict = {}
    stores.update(_graph_sections())
    stores["sqlite"] = _safe(_sqlite_section)
    stores["md_files"] = _safe(_md_section)
    stores["chat"] = _safe(_chat_section)
    stores["sources"] = _safe(_sources_section)
    return {"backend": backend_select.memory_backend(), "stores": stores}


# ─────────────────────── graph sample (removed backend) ─────────────────────

def graph_expand(store: str, node_id: str, limit: int = 40) -> dict:
    """Neighborhood of ONE node in a graph store.

    The graph visualization backend was removed (SQLite-only build), so this
    always returns the empty/unavailable shape — same contract the UI already
    handled on SQLite deployments.
    """
    return {"available": False, "nodes": [], "edges": []}


def graph_sample(store: str, limit: int = 60) -> dict:
    """Small node-link sample of ONE graph store.

    The graph visualization backend was removed (SQLite-only build), so this
    always returns the empty/unavailable shape — same contract the UI already
    handled on SQLite deployments.
    """
    return {"available": False, "nodes": [], "edges": []}


# ──────────────────────────── per-store clear ───────────────────────────────

def _clear_md() -> int:
    """Delete per-run captures (``captures/`` + any legacy root copies) AND the
    consolidated briefs in the ``compacted/`` subfolder. (Both moved out of the
    root into subfolders, so a root-only glob no longer wipes them — that left
    stale files after a clear / --clear re-ingest.) The archive/ and
    memory-archive/ trees are preserved."""
    from aiforge_core.memory import md_store
    n = 0
    for p in list(md_store.memory_dir().glob("*.md")) \
            + list(md_store.captures_dir().glob("*.md")) \
            + list(md_store.briefs_dir().glob("*.md")):
        if p.is_file():
            p.unlink()
            n += 1
    return n


def _clear_target(store: str) -> dict:
    """Return the human-readable description of what a clear deletes, so the
    result (and the confirm dialog) can state it exactly."""
    if store in _GRAPH_STORES:
        return {"backend": "removed"}
    if store == "sqlite":
        return {"backend": "sqlite", "table": "memory_units"}
    if store == "chat":
        return {"backend": "sqlite/postgres", "table": "chat_sessions+messages"}
    if store == "md_files":
        return {"backend": "disk", "match": "*.md under memory dir"}
    return {}


def clear_store(store: str) -> dict:
    """Delete all DATA in ONE store (idempotent, soft-fails per store).

    ``store`` ∈ ``graph_facts | symbols | graphify | chunks | sqlite |
    md_files | chat``. Unknown store → ValueError (the API maps that to 400).
    Never touches registered sources or configuration. The graph stores are a
    removed backend, so clearing one is a no-op (deleted=0).
    """
    if store not in CLEARABLE:
        raise ValueError(f"unknown store: {store}")
    try:
        if store in _GRAPH_STORES:
            deleted = 0  # graph backend removed — nothing to clear
        elif store == "sqlite":
            from aiforge_core.memory import sqlite_memory
            deleted = sqlite_memory.clear()
        elif store == "chat":
            from aiforge_core.runtime import chat_store
            deleted = chat_store.delete_all_sessions()
        elif store == "md_files":
            deleted = _clear_md()
        else:  # pragma: no cover — guarded by CLEARABLE
            raise ValueError(f"unhandled store: {store}")
        return {"store": store, "ok": True, "deleted": int(deleted),
                "target": _clear_target(store), "note": _PRESERVE_NOTE}
    except Exception as exc:  # noqa: BLE001
        return {"store": store, "ok": False, "reason": str(exc),
                "target": _clear_target(store)}


def clear_all() -> dict:
    """Wipe DATA across every store, then reset the still-registered sources'
    index state to idle/units=0 (registrations + config are preserved). Returns
    a per-store result dict; nothing raises."""
    results = {s: clear_store(s) for s in CLEARABLE}
    try:
        from aiforge_core.runtime import memory_sources
        sources_reset = memory_sources.reset_all()
    except Exception as exc:  # noqa: BLE001
        sources_reset = {"ok": False, "reason": str(exc)}
    return {"results": results, "sources_reset": sources_reset,
            "note": _PRESERVE_NOTE}


__all__ = ["memory_overview", "graph_sample", "graph_expand", "clear_store",
           "clear_all", "CLEARABLE"]
