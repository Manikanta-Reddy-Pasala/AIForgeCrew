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
    temporary: bool = Field(False, description="A temporary chat: nothing of it is "
                            "learned, captured, traced or kept; it runs read-only "
                            "and is deleted when it is closed (runtime/chat_private).")


class _RenameBody(BaseModel):
    title: str = Field(..., min_length=1)


class _LearnBody(BaseModel):
    learn: bool = Field(..., description="false = write no memory from this chat")


def _chat_workspace_root() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_WORKSPACE_ROOT",
        os.path.join(os.path.expanduser(
            str(config_dir())), "chat-workspaces"))


def _wipe_temporary(session_id: int) -> None:
    """A temporary chat is gone: also its own scratch folder (media lives
    there even when the chat was opened on a project), a running-turn
    snapshot and a trace file, should any exist."""
    import shutil
    from aiforge_core.config.paths import config_dir
    from aiforge_core.runtime import chat_private
    chat_private.forget(session_id)
    _delete_chat_workspace(os.path.join(_chat_workspace_root(), f"session-{session_id}"))
    cfg = str(config_dir())
    for p in (os.path.join(cfg, "running-turns", f"{session_id}.json"),
              os.path.join(cfg, "chat_traces", f"session_{session_id}.md"),
              os.path.join(cfg, "chat_traces", f"session_{session_id}.jsonl")):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(os.path.join(cfg, "chat_transcripts", f"session_{session_id}"),
                  ignore_errors=True)


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
    s = chat_store.create_session(body.title or ("Temporary chat" if body.temporary
                                                 else _NEW_CHAT),
                                  body.cwd or _default_cwd(),
                                  role=body.role or "chat",
                                  **({"temporary": True} if body.temporary else {}))
    from aiforge_core.runtime import chat_private
    chat_private.remember(s["id"], bool(body.temporary))
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
    # big repo is seconds, and the first turn must not wait on it. (Not for a
    # temporary chat: opening a project mirrors its memory into the repo.)
    if body.cwd and not body.temporary:
        try:
            from aiforge_core.runtime.chat_agent._context._repomap import (
                warm_repo_map)
            warm_repo_map(s.get("cwd") or body.cwd)
        except Exception:  # noqa: BLE001
            pass
        # A chat opened on a project folder: mirror its memory brief into the
        # repo's .aiforge folder and read its instruction files once.
        try:
            from aiforge_core.memory import projects as _projects
            _root = _projects.project_path_of(body.cwd)
            if _root:
                _projects.open_project(_root, background=True)
        except Exception:  # noqa: BLE001 — never blocks opening a chat
            pass
    return s


def _same_folder(cwd: str | None, want: str) -> bool:
    """Whether a session's cwd IS ``want`` or sits inside it."""
    if not cwd:
        return False
    a = os.path.normpath(str(cwd))
    b = os.path.normpath(want)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


@router.get("/api/chat/sessions")
def chat_session_list(cwd: str | None = None, no_project: bool = False) -> list[dict]:
    """Every chat, newest first. ``cwd`` keeps only the chats opened on that
    folder — a project's own list. ``no_project`` keeps only the chats that
    belong to no project (the "No repo" list)."""
    from aiforge_core.runtime import chat_store
    rows = chat_store.list_sessions()
    if cwd:
        rows = [r for r in rows if _same_folder(r.get("cwd"), cwd)]
    elif no_project:
        from aiforge_core.memory import projects as _projects
        _paths = _projects.known_paths()
        rows = [r for r in rows
                if not _projects.project_path_of(r.get("cwd"), _paths)]
    # Which chats have a run going right now, so the list can show it and the
    # user can open another chat beside a busy one.
    from aiforge_core.runtime import chat_runs
    return [{**r, "running": chat_runs.is_running(r["id"])} for r in rows]


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
    # A temporary chat's own files go too. Its id stays known as temporary
    # (a turn still finishing writes nothing) until a new chat takes the id.
    temps = [s["id"] for s in (chat_store.list_sessions() or [])
             if s.get("temporary") and not s.get("parent_id")]
    try:
        from aiforge_core.runtime import chat_worktree
        for _s in chat_store.list_sessions() or []:
            if not _s.get("parent_id"):
                from aiforge_core.runtime import team_run_life
                team_run_life.forget_session(_s["id"])
            if not _s.get("parent_id") and _s.get("workdir"):
                chat_worktree.remove(_s["id"])
    except Exception:  # noqa: BLE001
        pass
    deleted = chat_store.delete_all_sessions()
    # Wipe compaction offsets too — ids restart at 1 after a reset, so a leftover
    # marker would make the new session-1 skip folding (silent knowledge loss).
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.clear_all_markers()
    except Exception:  # noqa: BLE001
        pass
    for _tid in temps:
        _wipe_temporary(_tid)
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


@router.patch("/api/chat/sessions/{session_id}/learn", responses={404: {"description": "Not found"}})
def chat_session_set_learn(session_id: int, body: _LearnBody) -> dict:
    """Switch memory writes on or off for ONE chat. Off = nothing this chat
    says is learned, folded or captured; reading memory is unaffected."""
    from aiforge_core.runtime import chat_private, chat_store
    if body.learn and chat_private.is_temporary(session_id):
        raise HTTPException(409, "a temporary chat never learns — start a normal chat")
    s = chat_store.set_session_learn(session_id, body.learn)
    if not s:
        raise HTTPException(404, f"session {session_id} not found")
    return s


@router.delete("/api/chat/sessions/{session_id}/transcript", responses={404: {"description": "Not found"}, 409: {"description": "A turn is running"}})
def chat_session_fresh_context(session_id: int) -> dict:
    """Start the next message from a fresh context: the working transcript the
    chat carries between messages (runtime/chat_transcript) is dropped; the
    messages themselves stay. Refused while a turn runs — it would save its
    transcript again when it ends."""
    from aiforge_core.runtime import chat_runs, chat_store, chat_transcript
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    if chat_runs.is_running(session_id):
        raise HTTPException(409, "a turn is running in this chat — wait for it to end")
    chat_transcript.drop(session_id)
    return {"ok": True}


@router.post("/api/chat/sessions/{session_id}/compact-context", responses={404: {"description": "Not found"}})
def chat_session_compact_context(session_id: int) -> dict:
    """The Compact button: shrink the chat's working context now.

    A turn is running: it compacts at its next step and carries on with the
    same task (nothing is stopped). No turn is running: the context the chat
    carries to its next message is compacted; and when the last turn did not
    finish (stopped, or work was left), a turn that continues it is started
    (the UI attaches to it like to any running turn)."""
    from aiforge_core.runtime import chat_compact, chat_runs, chat_store
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    if chat_runs.is_running(session_id):
        chat_compact.request(session_id)
        return {"ok": True, "mode": "running",
                "message": "Compacting the context at the agent's next step — the "
                           "task goes on by itself."}
    got = chat_compact.compact_saved(session_id, role=sess.get("role") or "chat")
    out = {"mode": "idle", **got}
    if got.get("ok") and not got.get("compacted"):
        # A stopped task keeps no full working context: it is kept as its
        # handoff (goal, what is done, what failed, the next step) — already
        # the compact form. Say so, and show it.
        try:
            from aiforge_core.runtime import handoff_store
            v = handoff_store.view(session_id)
            if v.get("unfinished") and v.get("text"):
                out["handoff"] = True
                out["summary"] = v["text"][:4000]
                out["reason"] = ("this stopped task is already kept as a short summary "
                                 "(goal, what is done, the next step)")
        except Exception:  # noqa: BLE001 — the answer stands without it
            pass
    if got.get("ok") and chat_compact.last_turn_unfinished(session_id):
        try:
            from ._message import _SessionMsgBody, chat_session_message
            chat_session_message(session_id, _SessionMsgBody(
                content=chat_compact.RESUME_PROMPT, resume=True))
            out["resumed"] = True
        except Exception as exc:  # noqa: BLE001 — the compaction itself stands
            out["resumed"] = False
            out["resume_error"] = str(exc)[:200]
    return out


class _RecipeBody(BaseModel):
    verb: str
    cmd: str = ""
    cwd: str = "."
    aliases: list[str] | None = None


def _recipe_root(session_id: int) -> str:
    """The project whose recipes the session uses: the toplevel of the chat's
    worktree when it has one, else of its cwd (runtime/recipes)."""
    from aiforge_core.runtime import chat_store, chat_worktree, recipes
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = chat_worktree.workdir_of(sess) or sess.get("cwd") or _default_cwd()
    return recipes.lookup_root(cwd)


@router.get("/api/chat/sessions/{session_id}/recipes", responses={404: {"description": "Not found"}})
def chat_session_recipes(session_id: int) -> dict:
    """This project's command recipes: verb → {cmd, cwd, aliases, saved}."""
    from aiforge_core.runtime import recipes
    return {"ok": True, "recipes": recipes.load(_recipe_root(session_id))}


@router.post("/api/chat/sessions/{session_id}/recipes", responses={404: {"description": "Not found"}, 400: {"description": "Bad recipe"}})
def chat_session_recipe_save(session_id: int, body: _RecipeBody) -> dict:
    """Save (or replace) a recipe. The verb is canonicalised the way a recipe
    sentence is ("compile" → build)."""
    from aiforge_core.runtime import recipes
    if not body.cmd.strip():
        raise HTTPException(400, "cmd is required")
    root = _recipe_root(session_id)
    verb = recipes.canon(body.verb)
    try:
        row = recipes.save(root, verb, body.cmd, cwd=body.cwd, aliases=body.aliases)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "verb": verb, "recipe": row}


@router.post("/api/chat/sessions/{session_id}/recipes/decline", responses={404: {"description": "Not found"}, 400: {"description": "Bad verb"}})
def chat_session_recipe_decline(session_id: int, body: _RecipeBody) -> dict:
    """The user declined the offer for this verb: it is not offered again."""
    from aiforge_core.runtime import recipes
    root = _recipe_root(session_id)
    try:
        recipes.decline(root, recipes.canon(body.verb))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.delete("/api/chat/sessions/{session_id}/recipes/{verb}", responses={404: {"description": "Not found"}, 400: {"description": "Bad verb"}})
def chat_session_recipe_remove(session_id: int, verb: str) -> dict:
    """Drop a recipe; ``ok`` is False when there was none for the verb."""
    from aiforge_core.runtime import recipes
    root = _recipe_root(session_id)
    try:
        return {"ok": recipes.remove(root, recipes.canon(verb))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/chat/sessions/{session_id}", status_code=204, responses={404: {"description": "Not found"}})
def chat_session_delete(session_id: int) -> None:
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_runs,
        chat_store,
    )
    # A chat's side tasks go with it. Each is a session of its own, so the same
    # delete (stop its run, fold, drop its workspace) applies.
    for _child in chat_store.child_sessions(session_id):
        try:
            chat_session_delete(_child["id"])
        except HTTPException:
            pass
    # Stop any in-flight run first so its background producer doesn't keep
    # running + persisting against a session that no longer exists.
    chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)
    chat_interject.clear(session_id)
    chat_runs.finish(session_id)
    from aiforge_core.runtime import chat_private
    if chat_private.is_temporary(session_id):
        # nothing of a temporary chat outlives it, a watch or command included
        try:
            from aiforge_core.runtime import bg_work
            bg_work.stop_session(session_id)
        except Exception:  # noqa: BLE001
            pass
    from aiforge_core.runtime import chat_transcript
    chat_transcript.drop(session_id)
    try:
        from aiforge_core.runtime import chat_lessons
        chat_lessons.forget(session_id)
    except Exception:  # noqa: BLE001
        pass
    from aiforge_core.runtime import proc_logs
    proc_logs.forget_session(session_id)
    # Grab the isolated-workspace path BEFORE deleting the row so we can rm -rf
    # it — a lingering workspace's files otherwise leak into a future chat.
    _sess = chat_store.get_session(session_id)
    # Fold this session's knowledge into memory BEFORE its rows go — otherwise
    # deleting a chat silently discards everything worked out in it. Blocking +
    # idempotent + never raises; skip via AIFORGE_SESSION_COMPACT_ON_SWITCH=0.
    from aiforge_core.runtime import chat_session_fold
    if chat_session_fold._enabled():
        chat_session_fold.fold_sync(session_id)
    # Its own worktree goes with it; the branch (and so every commit) stays.
    # A team run parked for this chat (another repo's worktree) goes too.
    try:
        from aiforge_core.runtime import chat_worktree, team_run_life
        team_run_life.forget_session(session_id)
        chat_worktree.remove(session_id)
    except Exception:  # noqa: BLE001 — never blocks a delete
        pass
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")
    # Drop the session's compaction-offset marker so the marker file doesn't
    # accumulate entries for deleted sessions.
    try:
        from aiforge_core.runtime import chat_okr
        chat_okr.forget_session(session_id)
    except Exception:  # noqa: BLE001
        pass
    # A side task shares its parent's folder — never remove that with it.
    if not (_sess or {}).get("parent_id"):
        _delete_chat_workspace((_sess or {}).get("cwd"))
        if (_sess or {}).get("temporary"):
            _wipe_temporary(session_id)
        else:
            from aiforge_core.runtime import chat_private as _cp
            _cp.forget(session_id)


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
    from aiforge_core.runtime import request_context
    # The description's model call belongs to this chat (a temporary chat's
    # upload is then not traced either).
    _tok = request_context.set_session_id(session_id)
    try:
        # describe_upload runs a (slow) vision/text extraction — off the event
        # loop so one image upload doesn't block every other request.
        desc = await asyncio.to_thread(
            chat_media.describe_upload, saved["path"], saved["filename"],
            saved["mime"], role)
    except Exception:  # noqa: BLE001 — describe/extract is best-effort
        desc = ""
    finally:
        request_context.reset_session_id(_tok)
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


@router.get("/api/chat/captures/{capture_id}", responses={404: {"description": "Not found"}})
def chat_capture_raw(capture_id: str) -> FileResponse:
    """A screenshot ``ui_check`` / ``browse screenshot`` took, so the chat can
    SHOW it. ``capture_path`` refuses any id carrying a path separator."""
    from aiforge_core.runtime.visual import capture_path
    path = capture_path(capture_id)
    if path is None:
        raise HTTPException(404, "capture not found")
    return FileResponse(path, media_type="image/png")


@router.get("/api/chat/media/{media_id}/raw", responses={404: {"description": "Not found"}})
def chat_media_raw(media_id: int) -> FileResponse:
    from aiforge_core.runtime import chat_store
    row = chat_store.get_media(media_id)
    if row is None or not os.path.isfile(row.get("path") or ""):
        raise HTTPException(404, "media not found")
    return FileResponse(row["path"], media_type=row.get("mime") or "image/png")
