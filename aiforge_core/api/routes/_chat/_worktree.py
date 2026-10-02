"""A chat's own git worktree: what it holds, and merging it back.

See :mod:`aiforge_core.runtime.chat_worktree`.
"""
from __future__ import annotations

from fastapi import HTTPException

from ._core import router


def _family(session_id: int) -> "dict":
    """The chat that owns the worktree: a side task uses its parent's."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    if sess.get("parent_id"):
        return chat_store.get_session(sess["parent_id"]) or sess
    return sess


@router.get("/api/chat/sessions/{session_id}/worktree",
            responses={404: {"description": "Not found"}})
def chat_worktree_info(session_id: int) -> dict:
    """Where this chat works, and how far ahead of the project's branch it is."""
    from aiforge_core.runtime import chat_worktree
    sess = _family(session_id)
    data = chat_worktree.info(sess)
    if not data:
        return {"active": False, "enabled": chat_worktree.enabled()}
    return {"active": True, "enabled": True, "branch": data["branch"],
            "base_branch": data["base_branch"], "ahead": data["ahead"],
            "uncommitted": len(data["uncommitted"]), "main_branch": data["main_branch"],
            "main_moved": data["main_moved"], "main_dirty": len(data["main_dirty"]),
            "path": data["path"], "repo": data["repo"]}


@router.post("/api/chat/sessions/{session_id}/worktree/merge",
             responses={404: {"description": "Not found"}, 409: {"description": "Conflict"}})
def chat_worktree_merge(session_id: int) -> dict:
    """Bring this chat's commits onto the project's branch (fast-forward only;
    the chat's own branch is rebased first if the project's moved)."""
    from aiforge_core.runtime import chat_runs, chat_worktree
    sess = _family(session_id)
    if chat_runs.is_running(sess["id"]):
        raise HTTPException(409, "This chat is still working. Merge when it has finished.")
    return chat_worktree.merge(sess["id"])
