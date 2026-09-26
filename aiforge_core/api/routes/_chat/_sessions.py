"""Chat session CRUD, per-session workspaces, and media attachments."""
from __future__ import annotations

import asyncio
import os
from typing import Annotated

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from aiforge_core.config.paths import config_dir

from ._core import (
    _NEW_CHAT,
    _default_cwd,
    router,
)


class _NewSessionBody(BaseModel):
    title: str | None = Field(None)
    cwd: str | None = Field(None)
    role: str = Field("chat", description="model slot driving chat (default: chat)")


class _RenameBody(BaseModel):
    title: str = Field(..., min_length=1)


def _chat_workspace_root() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(os.path.expanduser(
            str(config_dir())), "chat-workspaces"))


def _delete_chat_workspace(cwd: str | None) -> bool:
    """``rm -rf`` a session's ISOLATED workspace when it is the managed,
    auto-created one under :func:`_chat_workspace_root`. Returns True if a dir
    was removed. Refuses anything else — a user-pinned project cwd, the root
    itself, or a path outside the managed tree — so clearing a chat can NEVER
    nuke a real repo. Leftover workspaces were the source of the "previous
    ticket's files leak into a new chat" bug; deleting them on clear removes it
    at the root (the per-turn baseline commit is the belt; this is the braces)."""
    if not cwd or not str(cwd).strip():
        return False
    import shutil
    try:
        root = os.path.realpath(_chat_workspace_root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return False
    # Must be STRICTLY inside the managed root, and a session-* dir — never the
    # root itself, never a pinned repo, never a traversal escape.
    if target == root or not target.startswith(root + os.sep):
        return False
    if not os.path.basename(target).startswith("session-"):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def _is_isolated_workspace(cwd: str | None) -> bool:
    """True when ``cwd`` is a session's auto-created isolated scratch workspace
    (``chat-workspaces/session-<id>``) — NOT a real project. Such a session must
    not mint a phantom ``projects/session-<id>/`` OKR scope; its knowledge is
    GLOBAL. Same containment check as :func:`_delete_chat_workspace`."""
    if not cwd or not str(cwd).strip():
        return False
    try:
        root = os.path.realpath(_chat_workspace_root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return False
    return (target != root and target.startswith(root + os.sep)
            and os.path.basename(target).startswith("session-"))


@router.post("/api/chat/sessions", status_code=201)
def chat_session_create(body: _NewSessionBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.create_session(body.title or _NEW_CHAT,
                                  body.cwd or _default_cwd(),
                                  role=body.role or "chat")
    # Isolation: when the caller didn't pin a cwd, give the session its
    # own workspace dir so it can build/clean/run without touching other
    # sessions or the host. Persisted under app_state on the compose deploy.
    if not body.cwd:
        ws = os.path.join(_chat_workspace_root(), f"session-{s['id']}")
        try:
            os.makedirs(ws, exist_ok=True)
            s = chat_store.set_session_cwd(s["id"], ws) or s
        except OSError:
            pass
    # Opening a NEW chat = moving away from the previous one — fold that prior
    # session into memory in the background so its knowledge is recalled here.
    try:
        from aiforge_core.runtime import chat_session_fold
        chat_session_fold.fold_previous_async(s["id"])
    except Exception:  # noqa: BLE001 — a fold must never break session create
        pass
    # Identify the chat model's vision capability NOW (background), so it's known
    # before the user attaches an image — not discovered only on first upload.
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.warm_vision_async(s.get("role") or "chat")
    except Exception:  # noqa: BLE001
        pass
    # Start the ranked repo map for a pinned project now: the first parse of a
    # big repo is seconds, and the first turn must not wait on it.
    if body.cwd:
        try:
            from aiforge_core.runtime.chat_agent._context._repomap import (
                warm_repo_map)
            warm_repo_map(s.get("cwd") or body.cwd)
        except Exception:  # noqa: BLE001
            pass
    return s


@router.get("/api/chat/sessions")
def chat_session_list() -> list[dict]:
    from aiforge_core.runtime import chat_store
    return chat_store.list_sessions()


def _sweep_orphan_session_dirs() -> int:
    """Belt-and-braces: rm -rf any orphaned ``session-*`` dirs under the managed
    workspace root (e.g. from a session whose row was already gone). Returns how
    many were removed. Never raises."""
    removed = 0
    try:
        import shutil
        root = _chat_workspace_root()
        for name in os.listdir(root):
            if not name.startswith("session-"):
                continue
            path = os.path.join(root, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return removed


@router.post("/api/chat/sessions/reset")
def chat_sessions_reset() -> dict:
    """Delete ALL chat sessions + messages and reset the id sequence, AND rm -rf
    every managed session workspace so no stale files survive the clear."""
    from aiforge_core.runtime import chat_store
    # Snapshot each session's cwd before the rows go, so we delete exactly the
    # managed workspaces they owned (a pinned user repo is refused by the helper).
    cwds = [(s or {}).get("cwd") for s in (chat_store.list_sessions() or [])]
    deleted = chat_store.delete_all_sessions()
    # Wipe compaction offsets too — ids restart at 1 after a reset, so a leftover
    # marker would make the new session-1 skip folding (silent knowledge loss).
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.clear_all_markers()
    except Exception:  # noqa: BLE001
        pass
    removed = sum(1 for _cwd in cwds if _delete_chat_workspace(_cwd))
    removed += _sweep_orphan_session_dirs()
    return {"ok": True, "deleted": deleted, "workspaces_removed": removed}


@router.get("/api/chat/sessions/{session_id}", responses={404: {"description": "Not found"}})
def chat_session_get(session_id: int) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return {"session": s, "messages": chat_store.get_messages(session_id)}


@router.post("/api/chat/sessions/{session_id}/compact", responses={404: {"description": "Not found"}})
def chat_session_compact(session_id: int) -> dict:
    """Session-end OKR compaction (explicit trigger): distil this session into
    scoped OKR briefs (global / project / topic) via chat_okr.compact_session."""
    from aiforge_core.runtime import chat_okr, chat_store
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = sess.get("cwd")
    repo = _chat_repo_key(cwd) if cwd else None
    return chat_okr.compact_session(session_id, repo=repo)


@router.get("/api/chat/sessions/{session_id}/trace")
def chat_session_trace(session_id: int) -> dict:
    """Reviewable per-turn action+response trace (from ~/.aiforge/chat_traces).
    Each turn = {ts, mode, prompt, actions[], response, n_tools}."""
    from aiforge_core.runtime import chat_trace
    turns = chat_trace.read_turns(session_id)
    return {"session_id": session_id, "count": len(turns), "turns": turns}


@router.get("/api/chat/sessions/{session_id}/llm-usage")
def chat_session_llm_usage(session_id: int) -> dict:
    """How many requests this chat has sent to the LLM.

    ``turn`` = since the current/most recent turn started, ``session`` = since
    the API started, ``per_minute`` = machine-wide rate over the last 60s (what
    a rate-limited provider — and the user's fan — actually feels). Counted at
    the wire, so retries and fallbacks count, and reset on API restart.
    """
    from aiforge_core.llm import call_meter
    return {"session_id": session_id, **call_meter.snapshot(session_id)}


@router.get("/api/chat/sessions/{session_id}/spec")
def chat_session_spec(session_id: int) -> dict:
    """The planner's SPEC.md (requirements + subtask breakdown) for this
    session's workspace — rendered as a markdown preview in the subtask dock."""
    from aiforge_core.runtime import chat_store
    sess = chat_store.get_session(session_id) or {}
    cwd = sess.get("cwd") or _default_cwd()
    path = os.path.join(cwd, "SPEC.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return {"exists": True, "path": path, "content": fh.read()[:200000]}
    except Exception as exc:  # noqa: BLE001
        return {"exists": False, "error": str(exc)}
    return {"exists": False, "content": ""}


@router.patch("/api/chat/sessions/{session_id}", responses={404: {"description": "Not found"}})
def chat_session_rename(session_id: int, body: _RenameBody) -> dict:
    from aiforge_core.runtime import chat_store
    s = chat_store.rename_session(session_id, body.title)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return s


@router.delete("/api/chat/sessions/{session_id}", status_code=204, responses={404: {"description": "Not found"}})
def chat_session_delete(session_id: int) -> None:
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_runs,
        chat_store,
    )
    # Stop any in-flight run first so its background producer doesn't keep
    # running + persisting against a session that no longer exists.
    chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)
    chat_interject.clear(session_id)
    chat_runs.finish(session_id)
    # Grab the isolated-workspace path BEFORE deleting the row so we can rm -rf
    # it — a lingering workspace's files otherwise leak into a future chat.
    _sess = chat_store.get_session(session_id)
    # Fold this session's knowledge into memory BEFORE its rows go — otherwise
    # deleting a chat silently discards everything worked out in it. Blocking +
    # idempotent + never raises; skip via AIFORGE_SESSION_COMPACT_ON_SWITCH=0.
    from aiforge_core.runtime import chat_session_fold
    if chat_session_fold._enabled():
        chat_session_fold.fold_sync(session_id)
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    # Drop the session's compaction-offset marker so the marker file doesn't
    # accumulate entries for deleted sessions.
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.forget_session(session_id)
    except Exception:  # noqa: BLE001
        pass
    _delete_chat_workspace((_sess or {}).get("cwd"))


@router.post("/api/chat/sessions/{session_id}/media", status_code=201, responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
async def chat_media_upload(session_id: int, file: Annotated[UploadFile, File()]) -> dict:
    """Attach a file (image OR document — pdf/xlsx/docx/text) to a chat session:
    save it to the session's media folder, derive a description (vision caption
    for an image, extracted text for a document), and store the row. The
    description is what makes it queryable later in the session."""
    from aiforge_core.runtime import chat_media, chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    raw = await file.read()
    saved = chat_media.save_file(session_id, file.filename or "file", raw)
    if not saved.get("ok"):
        raise HTTPException(400, saved.get("error", "invalid file"))
    role = (chat_store.get_session(session_id) or {}).get("role") or "chat"
    try:
        # describe_upload runs a (slow) vision/text extraction — off the event
        # loop so one image upload doesn't block every other request.
        desc = await asyncio.to_thread(
            chat_media.describe_upload, saved["path"], saved["filename"],
            saved["mime"], role)
    except Exception:  # noqa: BLE001 — describe/extract is best-effort
        desc = ""
    row = chat_store.add_media(session_id, saved["filename"], saved["path"],
                               mime=saved["mime"], description=desc)
    row["kind"] = saved.get("kind")
    row["auto_described"] = bool(desc)
    return row


@router.get("/api/chat/sessions/{session_id}/media")
def chat_media_list(session_id: int) -> dict:
    from aiforge_core.runtime import chat_media, chat_store
    return {"media": chat_store.list_media(session_id),
            "vision": chat_media.vision_enabled(
                (chat_store.get_session(session_id) or {}).get("role") or "chat")}


class _MediaDescBody(BaseModel):
    description: str = Field("", description="user caption / edited description")


@router.patch("/api/chat/media/{media_id}", responses={404: {"description": "Not found"}})
def chat_media_describe(media_id: int, body: _MediaDescBody) -> dict:
    from aiforge_core.runtime import chat_store
    row = chat_store.set_media_description(media_id, body.description)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    return row


@router.delete("/api/chat/media/{media_id}", status_code=204, responses={404: {"description": "Not found"}})
def chat_media_delete(media_id: int) -> None:
    from aiforge_core.runtime import chat_store
    row = chat_store.delete_media(media_id)
    if row is None:
        raise HTTPException(404, f"media {media_id} not found")
    try:  # best-effort unlink the file
        if row.get("path") and os.path.isfile(row["path"]):
            os.remove(row["path"])
    except Exception:  # noqa: BLE001
        pass


@router.get("/api/chat/media/{media_id}/raw", responses={404: {"description": "Not found"}})
def chat_media_raw(media_id: int) -> FileResponse:
    from aiforge_core.runtime import chat_store
    row = chat_store.get_media(media_id)
    if row is None or not os.path.isfile(row.get("path") or ""):
        raise HTTPException(404, "media not found")
    return FileResponse(row["path"], media_type=row.get("mime") or "image/png")
