"""Backend selection + the backend-neutral public function API.

Every public signature + return shape is preserved from the pre-split SQLite
module; callers are unchanged.
"""
from __future__ import annotations

import threading

from ._helpers import _rank_search, _tokens
from ._sqlite import _SqliteChatStore

_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def _backend():
    """The chat backend — embedded SQLite (SQLite-only build), created once
    per process."""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = _SqliteChatStore()
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None


# ═══════════════════════════ public function API ═════════════════════════════
# Every signature + return shape is preserved from the pre-refactor SQLite
# module; callers are unchanged.

def create_session(title: str = "New chat", cwd: str | None = None,
                   role: str = "chat") -> dict:
    return _backend().create_session(title, cwd, role)


def set_session_cwd(session_id: int, cwd: str) -> "dict | None":
    return _backend().set_session_cwd(session_id, cwd)


def set_session_role(session_id: int, role: str) -> "dict | None":
    return _backend().set_session_role(session_id, role)


def list_sessions() -> list[dict]:
    return _backend().list_sessions()


def get_session(session_id: int) -> "dict | None":
    return _backend().get_session(session_id)


def get_messages(session_id: int) -> list[dict]:
    return _backend().get_messages(session_id)


def search_messages(query: str, *, limit: int = 6,
                    exclude_session: "int | None" = None) -> list[dict]:
    """Full-text-ish search over prior chat message CONTENT (all sessions except
    ``exclude_session``). Cheap + local: one indexed-ish scan, no LLM, no
    network. A message matches if its content contains ANY query token (tokens:
    lowercase alphanumeric, len>=3, common stopwords dropped). Ranked by
    (# distinct tokens matched desc, then recency desc). Returns up to ``limit``
    hits: ``[{"session_id","session_title","role","content","created_at"}]``
    with each content truncated to ~300 chars. Soft-fail: any error → []."""
    toks = _tokens(query)
    if not toks:
        return []
    try:
        rows = _backend()._search_candidates(toks, exclude_session)
    except Exception:  # noqa: BLE001 — search must never break a chat turn
        return []
    return _rank_search(rows, toks, limit)


def add_message(session_id: int, role: str, content: str,
                steps: "list | None" = None, mode: str = "simple",
                duration_s: "float | None" = None) -> int:
    return _backend().add_message(session_id, role, content, steps, mode,
                                  duration_s)


def set_message_checkpoint(message_id: int, sha: str) -> None:
    """Stamp the workspace checkpoint sha taken just before this message — so
    edit-resend can restore the tree to exactly that turn's state."""
    return _backend().set_message_checkpoint(message_id, sha)


def delete_messages_from(session_id: int, message_id: int) -> int:
    """Delete this message and every message after it in the session (by the
    stable autoincrement id ordering). Returns the number of rows removed.
    Used by edit-and-resend: truncate history at the edited turn before re-running."""
    return _backend().delete_messages_from(session_id, message_id)


def message_checkpoint(session_id: int, message_id: int) -> "str | None":
    """The checkpoint sha stamped on a given message, or None."""
    return _backend().message_checkpoint(session_id, message_id)


def rename_session(session_id: int, title: str) -> "dict | None":
    return _backend().rename_session(session_id, title)


def delete_session(session_id: int) -> bool:
    return _backend().delete_session(session_id)


def delete_all_sessions() -> int:
    """Delete EVERY chat session + its messages and reset the id sequence so new
    sessions start at 1. Returns the count of sessions deleted."""
    return _backend().delete_all_sessions()


def _session_signature(be, s: dict) -> str:
    """A content fingerprint of one session: title + cwd + role + the full
    message sequence (role+content). Two sessions with the same signature are
    duplicates."""
    import hashlib
    try:
        msgs = be.get_messages(s.get("id")) or []
    except Exception:  # noqa: BLE001
        msgs = []
    parts = [str(s.get("title") or ""), str(s.get("cwd") or ""),
             str(s.get("role") or "")]
    parts += [(m.get("role") or "") + ":" + (m.get("content") or "") for m in msgs]
    return hashlib.sha1("\x1e".join(parts).encode("utf-8", "replace"),
                        usedforsecurity=False).hexdigest()


def dedupe_sessions() -> dict:
    """Remove DUPLICATE chat sessions (e.g. from a non-idempotent migration that
    re-created every session each run). Two sessions are duplicates when they
    share title + cwd + role AND the exact same message sequence (role+content).
    Keeps the LOWEST id, deletes the rest + their messages. Returns
    {ok, removed, kept}. Soft-fail."""
    be = _backend()
    try:
        sessions = sorted(be.list_sessions(), key=lambda x: x.get("id") or 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    seen: dict[str, int] = {}
    removed = 0
    for s in sessions:
        sig = _session_signature(be, s)
        if sig not in seen:
            seen[sig] = s.get("id")
            continue
        try:
            if be.delete_session(s.get("id")):
                removed += 1
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "removed": removed, "kept": len(seen)}


# ── Chat media (uploaded images + their descriptions) ─────────────────────────

def add_media(session_id: int, filename: str, path: str,
              mime: str = "", description: str = "") -> dict:
    return _backend().add_media(session_id, filename, path, mime, description)


def list_media(session_id: int) -> list[dict]:
    return _backend().list_media(session_id)


def get_media(media_id: int) -> "dict | None":
    return _backend().get_media(media_id)


def set_media_description(media_id: int, description: str) -> "dict | None":
    return _backend().set_media_description(media_id, description)


def delete_media(media_id: int) -> "dict | None":
    """Delete the row, returning it (so the caller can unlink the file)."""
    return _backend().delete_media(media_id)
