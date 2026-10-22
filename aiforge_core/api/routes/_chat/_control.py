"""Session control endpoints: checkpoints, promote to ticket, and next-step
suggestion feedback."""
from __future__ import annotations

import json
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from aiforge_core.tickets import store as tickets_mod

from ._core import (
    _NEW_CHAT,
    _default_cwd,
    router,
)


class _CheckpointBody(BaseModel):
    label: str | None = Field(None, description="human label for the snapshot")


@router.get("/api/chat/sessions/{session_id}/checkpoints", responses={404: {"description": "Not found"}})
def chat_session_checkpoints(session_id: int) -> dict:
    """List workspace checkpoints (#3) for this session's working dir."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return {"checkpoints": checkpoints.list_checkpoints(cwd)}


@router.post("/api/chat/sessions/{session_id}/checkpoints", status_code=201, responses={404: {"description": "Not found"}})
def chat_session_checkpoint_create(session_id: int, body: _CheckpointBody) -> dict:
    """Snapshot the session's working dir (#3) to a hidden git ref."""
    import datetime as _dt

    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return checkpoints.snapshot(cwd, label=body.label or "manual", when=when)


class _RestoreBody(BaseModel):
    sha: str = Field(..., min_length=4)
    paths: list[str] | None = Field(
        None, description="restore ONLY these paths (files-only / subset restore); "
                          "omit to restore the whole snapshot")
    delete_orphans: bool = Field(
        False, description="full-state restore: also delete files created after "
                           "the checkpoint so the tree exactly matches it")


@router.post("/api/chat/sessions/{session_id}/checkpoints/restore", responses={404: {"description": "Not found"}})
def chat_session_checkpoint_restore(session_id: int, body: _RestoreBody) -> dict:
    """Restore the session's working dir to a checkpoint (#3).

    Granularity: ``paths`` restores a subset; ``delete_orphans`` makes it a
    full-state restore (matching the snapshot exactly)."""
    from aiforge_core.runtime import chat_store, checkpoints
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = session.get("cwd") or _default_cwd()
    return checkpoints.restore(cwd, body.sha, paths=body.paths or None,
                               delete_orphans=bool(body.delete_orphans))


class _RevertBody(BaseModel):
    paths: list[str] = Field(..., description="paths the scope net named, relative to the work folder")
    head0: str | None = Field(None, description="the commit the turn started on (the scope_net event's "
                                                "head0); a chat with a worktree needs it")


def _revert_target(root: str, path: str) -> "str | None":
    """The file ``path`` names inside ``root``, or None when the revert must
    not touch it: an empty or absolute path, one that leaves the root (every
    link on the way resolved), a symlink, a folder, or a pattern — the path
    is used literally, one plain file at a time. Nothing under ``.git`` (in
    a worktree ``.git`` is a plain file). A file no longer on disk passes
    (its restore from ``ref`` is the revert)."""
    if (not path or os.path.isabs(path) or any(c in path for c in "*?[")
            or path != os.path.normpath(path)
            or ".git" in path.replace(os.sep, "/").split("/")):
        # The same spelling goes to git: "src/../a.py" is inside the root
        # but names nothing in a tree, and would read as a file to delete.
        return None
    lexical = os.path.join(root, path)
    real = os.path.realpath(lexical)
    if not real.startswith(root + os.sep) or os.path.islink(lexical):
        return None
    if os.path.lexists(lexical) and not os.path.isfile(lexical):
        return None
    return lexical


def _git_ok(root: str, *args) -> bool:
    import subprocess
    return subprocess.run(["git", "--literal-pathspecs", *args], cwd=root,
                          capture_output=True).returncode == 0


def _offered_paths(session_id: int, head0: "str | None") -> set:
    """The paths the scope net offered for revert in this session's answers
    (persisted ``scope_net`` steps) for the turn that started on ``head0``.
    Only these may be reverted: the request body names, the server decides."""
    from aiforge_core.runtime import chat_store
    want = (head0 or "").strip() or None
    out: set = set()
    for m in chat_store.get_messages(session_id) or []:
        steps = m.get("steps") or []
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except ValueError:
                steps = []
        for st in steps:
            if (isinstance(st, dict) and st.get("type") == "scope_net"
                    and ((st.get("head0") or None) == want)):
                out.update(p for p in (st.get("paths") or []) if isinstance(p, str))
    return out


def _revert_ref(root: str, head0: "str | None") -> "str | None":
    """``head0`` when it names a commit of the repo at ``root``, else None."""
    sha = (head0 or "").strip()
    if not sha or not sha.isalnum():
        return None
    return sha if _git_ok(root, "cat-file", "-e", f"{sha}^{{commit}}") else None


@router.post("/api/chat/sessions/{session_id}/revert",
             responses={400: {"description": "head0 missing or unknown"},
                        404: {"description": "Not found"}, 409: {"description": "A turn is running"}})
def chat_session_revert(session_id: int, body: _RevertBody) -> dict:
    """Undo the unrequested changes the scope net listed, one plain file at a
    time: a file the turn's first commit (``head0``) held goes back to that
    content; one it did not hold is deleted (and untracked, when the chat's
    worktree already committed it at the end of the turn). Paths outside the
    chat's work folder, symlinks, folders and patterns are refused. A chat
    with a worktree refuses a missing or unknown ``head0`` (its HEAD already
    holds the turn's work); a plain folder falls back to HEAD; a folder
    outside git is refused. Only paths a persisted ``scope_net`` step of this
    session offered for that ``head0`` are touched. A click right after the
    answer waits for the turn's bookkeeping instead of a 409."""
    from aiforge_core.runtime import chat_runs, chat_store, chat_worktree
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    if not chat_runs.settle(session_id):
        raise HTTPException(409, "a turn is running")
    workdir = chat_worktree.workdir_of(session)
    root = os.path.realpath(workdir or session.get("cwd") or _default_cwd())
    if not _git_ok(root, "rev-parse", "--is-inside-work-tree"):
        # Outside git there is no record of what the turn began with: every
        # file would read as new and be deleted.
        raise HTTPException(400, "the work folder is not a git repository: nothing reverted")
    ref = _revert_ref(root, body.head0)
    if ref is None:
        if workdir:
            raise HTTPException(400, "head0 missing or not a commit of this chat's worktree: "
                                     "nothing reverted")
        ref = "HEAD"
    offered = _offered_paths(session_id, body.head0)
    reverted, refused = [], []
    for p in body.paths:
        lexical = _revert_target(root, p) if p in offered else None
        if lexical is None:
            refused.append(p)
            continue
        # "<rev>:./<p>" reads <p> relative to the work folder; "<rev>:<p>"
        # would read it from the repository top and miss every file of a
        # subfolder work folder — which then reads as new and is deleted.
        if _git_ok(root, "cat-file", "-e", f"{ref}:./{p}"):
            ok = _git_ok(root, "checkout", ref, "--", p)
        elif not os.path.lexists(lexical):
            refused.append(p)              # neither on disk nor at ref: nothing to revert
            continue
        else:
            # Not there when the turn began: off the disk, and out of the
            # index when the worktree's seal already committed it.
            ok = True
            if os.path.lexists(lexical):
                try:
                    os.remove(lexical)
                except OSError:
                    ok = False
            if ok and _git_ok(root, "ls-files", "--error-unmatch", "--", p):
                ok = _git_ok(root, "rm", "-q", "--cached", "--", p)
        (reverted if ok else refused).append(p)
    return {"ok": not refused, "reverted": reverted, "refused": refused}


class _SessionTicketBody(BaseModel):
    content: str = Field(..., min_length=1)
    project: str | None = Field(None, description="target repo; defaults to session cwd name")


@router.post("/api/chat/sessions/{session_id}/ticket", status_code=201, responses={404: {"description": "Not found"}})
def chat_session_ticket(session_id: int, body: _SessionTicketBody) -> dict:
    """Pipeline mode: turn a chat message into a real ticket that runs the
    full architect→planner→verifier→doer→feedback→learner pipeline. The
    runner picks it up (urgent priority → next); the chat UI streams live
    stage updates from ``/api/trace/{identifier}/stream``. Returns the
    created ticket identifier + trace stream path."""
    from aiforge_core.runtime import chat_store
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    project = (body.project or "").strip() or os.path.basename(
        os.path.normpath(session.get("cwd") or _default_cwd())) or None
    title = body.content.strip().splitlines()[0][:120] or "chat request"
    if (session.get("title") or _NEW_CHAT) == _NEW_CHAT:
        chat_store.rename_session(session_id, title)
    t = tickets_mod.create(
        title=title, body=body.content.strip(), project=project,
        priority="urgent", route="code",
        # interactive=chat → the runner's clarify step may ask questions
        # before running. Normal tickets omit this → static, no ask.
        metadata={"source": "chat", "chat_session_id": session_id,
                  "interactive": True},
    )
    chat_store.add_message(session_id, "user", body.content)
    chat_store.add_message(
        session_id, "assistant",
        f"Started pipeline run as **{t.identifier}** (project `{project or '—'}`). "
        f"Streaming stage updates…",
        [{"type": "ticket", "identifier": t.identifier, "project": project}],
    )
    return {"ticket": t.identifier, "ticket_id": t.id, "project": project,
            "trace_url": f"/api/tickets/{t.identifier}/events/stream"}


@router.post("/api/chat/suggestion/{prediction_id}")
async def suggestion_outcome(prediction_id: str, request: Request) -> dict:
    """Record what the user did with a predicted next step.

    BOTH answers are recorded. A feature that learns only from its successes
    drifts, and a dismissal is the clearer signal of the two — it says the
    prediction was wrong about this user, which is exactly what the next one
    needs to know.

    An unknown id is a no-op rather than a 404: a chip in a browser tab left
    open across a restart is not an error the user can do anything about.

    Accepting an OFFER deliberately does NOT execute anything here. The chip
    sends the action back as an ordinary chat message, so it passes through the
    same approval gates, the same tool policy and the same transcript as
    anything else the user asks for. A second execution path that bypassed
    those gates is the hole this feature must not open.
    """
    from aiforge_core.runtime import next_step

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — a bodyless click is a dismissal
        payload = {}
    accepted = bool((payload or {}).get("accepted"))
    next_step.outcome(prediction_id, accepted,
                      edited=str((payload or {}).get("edited") or ""))
    return {"ok": True, "accepted": accepted}


@router.get("/api/chat/suggestions")
def suggestion_history(limit: int = 20) -> dict:
    """What has been predicted and what the user did with it.

    The counters that answer "is this feature good enough to extend to the
    pipeline" — which is the decision the design deliberately left open.
    """
    from aiforge_core.runtime import next_step

    rows = next_step.history(max(1, min(int(limit or 20), 200)))
    return {"suggestions": rows,
            "accepted": sum(1 for r in rows if r.get("accepted") is True),
            "dismissed": sum(1 for r in rows if r.get("accepted") is False)}
