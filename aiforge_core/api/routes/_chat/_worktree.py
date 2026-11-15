"""A chat's own git worktree: what it holds, and merging it back.

See :mod:`aiforge_core.runtime.chat_worktree`.
"""
from __future__ import annotations

import os

from fastapi import HTTPException
from pydantic import BaseModel

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
            "path": data["path"], "repo": data["repo"],
            "held_out": data.get("held_out") or []}


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


# ── where a project's chats work (asked once per project) ───────────────────

class WorktreePrefBody(BaseModel):
    repo: str
    place: str


def _repo_arg(repo: "str | None") -> str:
    if not repo or not os.path.isabs(os.path.expanduser(repo)):
        raise HTTPException(400, "repo must be an absolute path")
    return os.path.realpath(os.path.expanduser(repo))


@router.get("/api/chat/worktree-prefs", responses={400: {"description": "Bad request"}})
def chat_worktree_prefs_get(repo: "str | None" = None) -> dict:
    """Every project's saved choice, or one project's (``?repo=``). ``env``
    is the place an explicit env forces for every project (then nothing is
    asked), or null."""
    from aiforge_core.runtime import worktree_prefs as wp
    if repo:
        key = _repo_arg(repo)
        return {"repo": key, "pref": wp.get(key), "env": wp.env_place(),
                "effective": wp.place_for(key)}
    return {"prefs": wp.all_prefs(), "env": wp.env_place(), "ask": wp.ask_enabled()}


@router.put("/api/chat/worktree-prefs", responses={400: {"description": "Bad request"}})
def chat_worktree_prefs_put(body: WorktreePrefBody) -> dict:
    """Set where chats of ``repo`` work: ``repo`` | ``config`` | ``none``."""
    from aiforge_core.runtime import worktree_prefs as wp
    key = _repo_arg(body.repo)
    if body.place not in wp.PLACES:
        raise HTTPException(400, f"place must be one of {', '.join(wp.PLACES)}")
    return {"repo": key, "pref": wp.set_place(key, body.place)}


@router.delete("/api/chat/worktree-prefs", responses={400: {"description": "Bad request"}})
def chat_worktree_prefs_delete(repo: str) -> dict:
    """Forget the project's choice: the next new chat there asks again."""
    from aiforge_core.runtime import worktree_prefs as wp
    key = _repo_arg(repo)
    return {"repo": key, "cleared": wp.clear(key)}


@router.get("/api/chat/worktrees/old", responses={400: {"description": "Bad request"}})
def chat_worktrees_old(repo: str) -> dict:
    """The project's OLD AIForge worktrees (read only; removing them is a
    chat reply, "delete worktrees 1,3")."""
    from aiforge_core.runtime import worktree_inventory as inv
    key = _repo_arg(repo)
    if not os.path.isdir(key):
        raise HTTPException(400, "repo is not a folder")
    return {"repo": key, "old": inv.old_entries(key)}
