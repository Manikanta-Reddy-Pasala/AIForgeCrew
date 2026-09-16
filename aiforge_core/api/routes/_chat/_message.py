"""Message, attach, stop, kill-all, and steer endpoints."""
from __future__ import annotations

import json
import os

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aiforge_core.api.routes._sse import sse_response
from aiforge_core.runtime.background import spawn as _spawn
from aiforge_core.tickets import store as tickets_mod

from ._core import (
    _NEW_CHAT,
    _default_cwd,
    router,
)
from ._history import (
    _chat_history_for_agent,
)
from ._prep import (
    _apply_edit_resend,
    _apply_provisional_title,
    _apply_resume_brief,
    _expand_slash_command,
    _gen_title,
    _rehome_context_workspace,
)
from ._producer import (
    _produce,
    _stream,
)
from ._sessions import (
    _SessionMsgBody,
)


@router.post("/api/chat/sessions/{session_id}/message", responses={404: {"description": "Not found"}, 409: {"description": "Conflict"}})
def chat_session_message(session_id: int, body: _SessionMsgBody) -> StreamingResponse:
    """Append a user message, run the full-FS coding agent over the whole
    session history (Claude-CLI-style: many tool steps, builds repos),
    stream every step as SSE, and persist the assistant reply + steps.
    Auto-titles a fresh session. The model is the session's role
    (model picker)."""
    from aiforge_core.runtime import chat_store

    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")

    # Reject an overlapping run for the same session (two tabs, or a reattach
    # racing a send). Letting a 2nd producer start would replace this session's
    # cancel/approve token, so the 1st run's Stop becomes a no-op and BOTH
    # producers persist a turn (duplicate/garbled history). The client already
    # guards on `busy`; this is the server-side backstop. Use /attach to watch
    # the in-flight run instead.
    from aiforge_core.runtime import chat_runs
    if chat_runs.is_running(session_id) and not chat_runs.settle(session_id):
        raise HTTPException(409, "a run is already in progress for this session "
                                 "— stop it or attach to it before sending again")

    role = body.role or session.get("role") or "chat"
    if body.role and body.role != session.get("role"):
        chat_store.set_session_role(session_id, body.role)

    # Edit-and-resend: when the client edits an earlier user turn, restore the
    # workspace to that turn's checkpoint (so the re-run starts from the same
    # state the original did) and truncate the conversation at that message
    # before appending the edited content. Best-effort restore — a missing
    # checkpoint just means history truncation without a workspace rollback.
    _apply_edit_resend(session, session_id, body)
    # Custom slash commands (Claude Code / Cursor parity, LOCAL files only).
    # A leading "/<name> args" whose <name> matches a user-defined command file
    # (.aiforge/commands/<name>.md or .claude/commands/<name>.md) expands to that
    # markdown template with $ARGUMENTS/$1.. substituted. Do it HERE — before the
    # message is persisted, titled, folded into `history`, or read as `prompt` —
    # so ONE interception covers simple, plan AND team modes (all of them derive
    # their prompt from body.content / the persisted history downstream). A
    # non-command message, a "/" typo, or an unknown /name expands to None and is
    # left verbatim. The built-in /help (and /commands) needs no user file and is
    # answered inline without invoking the model. Fail-open: any error → raw text.
    _cmd_expanded, _cmd_help_text = _expand_slash_command(session, body)
    # Persist the run mode on the user turn so the UI can badge which mode each
    # turn/session ran in (was composer-only client state, never stored).
    _turn_mode = body.mode if body.mode in ("simple", "plan", "team") else "simple"
    import time as _time
    _turn_t0 = _time.time()   # wall-clock start → per-turn duration (all 3 modes)
    _user_msg_id = chat_store.add_message(session_id, "user", body.content,
                                          mode=_turn_mode)
    # Provisional title now (instant), upgraded to a model-generated one after
    # the turn (see _produce). _fresh marks a still-unnamed session.
    _fresh_title = (session.get("title") or _NEW_CHAT) == _NEW_CHAT
    _apply_provisional_title(session_id, body, _fresh_title)

    # Fold each assistant turn's tool digest into history + keep did-work-but-
    # blank turns + merge same-role runs, so the agent remembers what it DID
    # (not just what it said) on follow-ups.
    _rows = chat_store.get_messages(session_id)
    history = _chat_history_for_agent(_rows)
    cwd = session.get("cwd") or _default_cwd()
    team = body.mode == "team"
    agent_mode = "plan" if body.mode == "plan" else "act"
    prompt = body.content.strip()

    # RESUME. A retry after a STOPPED turn used to re-run the request from
    # nothing: the agent re-read what it had read and re-wrote what it had
    # written, then usually died in the same place. The failed attempt's work
    # is on disk — this hands the model the inventory of it (what landed, what
    # is still pending, what it failed on) so the retry finishes the remainder
    # instead of repeating the whole job. Automatic when the same words are
    # re-sent (what the Retry button does); `resume: true` forces it when the
    # user rephrased. Empty string when the last turn finished normally, so a
    # normal follow-up is untouched.
    _resume_brief = _apply_resume_brief(_rows, prompt, cwd, body, history)
    # Context-keyed workspace: if this chat is about a durable context (a Jira
    # ticket key like PROJ-42, or a Confluence page) and the session is still on
    # an EPHEMERAL folder (the default/session-<id> scratch), switch its cwd to
    # the SHARED ~/.aiforge/work/<kind>/<key>/ folder — so that ticket's images,
    # pages and scratch persist across every session that touches it. A session
    # already pinned to a context or to a real repo the user chose is left as-is.
    cwd = _rehome_context_workspace(cwd, prompt, session_id)
    # Per-turn auto-route: once a team session has produced output, a small
    # follow-up ("rename that", "add a test") shouldn't re-run the whole heavy
    # pipeline (worktree + planner + verifier + slow Doer loop = minutes). A
    # cheap classify downgrades simple follow-ups to the fast single-agent
    # path. First team turn + genuinely complex follow-ups keep the pipeline.
    # Safe by default: any classifier failure leaves team=True. Disable with
    # AIFORGE_TEAM_AUTO_ROUTE=0.
    # NOTE: the actual classify call is deferred to the top of `_produce()`
    # (see below) — it's an LLM round-trip, and running it HERE, in the
    # synchronous request handler, delays the StreamingResponse from opening
    # at all: an unreachable/slow endpoint's retry+backoff chain (many
    # seconds) left the client with zero bytes and no ping, looking hung,
    # for a decision that only affects `_parallel_team` / `_events()` (both
    # only read once the background thread is already running).
    _auto_downgraded = False
    _parallel_team = False   # finalized in _produce(), once `team` is settled

    # Upgrade a freshly-named session to a concise MODEL-generated title,
    # CONCURRENTLY with the turn (a fast ~20-token call) so it neither blocks
    # the response nor lingers the stream. The client's post-turn session
    # refresh picks it up. Best-effort.
    if _fresh_title:
        _spawn(lambda: _gen_title(prompt, session_id), name="gen-title")

    from aiforge_core.runtime import chat_cancel
    chat_cancel.start(session_id)
    # Steering is accepted in every mode: simple/plan drain mid-run steers in the
    # ReAct loop; parallel folds them into SPEC.md (stream_parallel_team) to guide
    # the remaining subtasks + reconcile. (Sequential team clears them at end.)
    from aiforge_core.runtime import chat_interject as _chat_interject
    _chat_interject.set_steerable(session_id, True)
    # Gap D — arm/disarm the pre-apply review gate for this run. Cleared on
    # chat_approve.finish() in every termination path (simple/parallel here,
    # team in chat_pipeline), so it never leaks into the next turn. The
    # actual set_review_edits() call is deferred to the top of `_produce()`
    # (needs the post-classify `team` value — see the auto-route note above).


    # Records which path the run actually took, so the persistence gate below
    # matches. ``driver`` is True ONLY once the sequential team ADK driver
    # (chat_pipeline) has been launched — it self-persists and owns the run's
    # lifetime. Every other path (simple/plan, parallel, best-of-N, OR a team
    # run that crashes in the pre-stream orchestrator before the driver starts)
    # persists + cleans up inline here.
    _path = {"parallel": False, "driver": False}



    # The PRODUCER runs on a background daemon thread and publishes every event
    # into the per-session run registry (chat_runs). It NO LONGER yields to the
    # HTTP response, so a client that navigates away (aborting the fetch) can't
    # kill the run — the thread runs to completion and persists the full turn.
    # The HTTP response (and any later /attach) just SUBSCRIBES and tails the
    # buffer. This is the same survive-the-disconnect pattern team mode already
    # used internally, now applied to every mode. (chat_runs imported above for
    # the is_running concurrency guard.)
    run = chat_runs.start(session_id)
    import types as _types
    pc = _types.SimpleNamespace(
        _cmd_help_text=_cmd_help_text, body=body, history=history, cwd=cwd,
        role=role, session_id=session_id, _resume_brief=_resume_brief,
        _cmd_expanded=_cmd_expanded, prompt=prompt, _turn_t0=_turn_t0, team=team,
        _auto_downgraded=_auto_downgraded, _parallel_team=_parallel_team,
        _path=_path, agent_mode=agent_mode, _turn_mode=_turn_mode, run=run,
        _user_msg_id=_user_msg_id)

    _spawn(lambda: _produce(pc), name="chat-produce")


    return sse_response(_stream(pc), label=f"chat-session-{session_id}")


@router.get("/api/chat/sessions/{session_id}/attach")
def chat_session_attach(session_id: int) -> StreamingResponse:
    """Re-attach to an in-flight run after navigating back to the Chat view.

    Replays the run's buffered events (so the client rebuilds the live turn
    from the start — thoughts, tools, subtasks, the in-progress answer) and
    then tails live events to completion. If no run is in flight for this
    session, emits a single ``done`` immediately so the client knows there's
    nothing live to resume (and can just show the persisted history)."""
    from aiforge_core.runtime import chat_runs

    def _gen():
        # First event always tells the client whether there's a live run, so it
        # can decide to show progress (running) or just keep the persisted
        # history (not running) — no guessing from the event stream.
        run = chat_runs.get(session_id)
        running = bool(run and not run.done)
        _att = {"type": "attached", "running": running}
        if running and run is not None:
            _att["started_at"] = run.started_at   # epoch secs → true elapsed
        yield f"data: {json.dumps(_att)}\n\n"
        if not running or run is None:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        q = run.subscribe()
        for ev in chat_runs.iter_subscription(run, q):
            yield f"data: {json.dumps(ev)}\n\n"

    return sse_response(_gen(), label=f"chat-attach-{session_id}")


@router.post("/api/chat/sessions/{session_id}/stop")
def chat_session_stop(session_id: int) -> dict:
    """Stop the in-flight chat run for this session — signals the agent
    loop / ADK pipeline to halt and kills any subprocess groups it
    spawned (builds, test runs). Idempotent."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    active = chat_cancel.cancel(session_id)
    chat_approve.cancel(session_id)   # unblock any pending approval gate
    return {"stopped": active, "session_id": session_id}


@router.post("/api/chat/kill-all")
def chat_kill_all() -> dict:
    """Force-reset ALL in-flight chat state — the 'kill all' escape hatch.

    Recovers from a wedged run that left a session looking busy or made a new
    chat sit on 'waiting for another team run to finish' (the team run lock was
    held by a run that won't release it). Cancels every tracked run, clears the
    approval + steer gates, finishes every live-run buffer, and force-releases
    the team run-serialization lock. Idempotent and safe to hit any time."""
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_pipeline,
        chat_runs,
    )
    sessions = chat_cancel.cancel_all()
    for sid in sessions:
        chat_approve.cancel(sid)
        chat_approve.finish(sid)
        chat_interject.clear(sid)
        # NOTE: do NOT chat_cancel.finish(sid) here — that pops the cancel token
        # microseconds after cancel_all() set it, before the (slow, between-poll)
        # producer can observe it, so the run kept executing. Leave the token
        # SET; each run's own finally pops it once it has actually torn down.
    chat_runs.finish_all()
    lock_freed = chat_pipeline.force_release_run_lock()
    return {"killed": sessions, "count": len(sessions),
            "team_lock_released": lock_freed}


class _SteerBody(BaseModel):
    content: str = Field(..., description="mid-run guidance to fold in")


@router.post("/api/chat/sessions/{session_id}/steer")
def chat_session_steer(session_id: int, body: _SteerBody) -> dict:
    """Inject a steer message into the IN-FLIGHT run for this session WITHOUT
    stopping it (Gap A — mid-run steering). The message is queued and folded
    into the agent's working context at its next safe step, so the agent
    adjusts course mid-run. No-op (queued:false) for blank content.

    Drained by: simple/plan's ReAct loop, the parallel-team subtask loop
    (folds into SPEC.md), and the sequential team ADK driver's Doer/Refiner
    before_model callback (chat_steer_callback). Only best-of-N never
    drains, so steering there would queue a message no loop ever reads —
    detect that and report it unsupported rather than falsely claiming the
    steer was queued."""
    from aiforge_core.runtime import chat_interject
    # Atomic test-and-set: push() itself checks steerability under its lock, so
    # there's no window between the check and the enqueue for a run-end clear()
    # to slip a stale steer into the next turn (CC3).
    queued = chat_interject.push(session_id, body.content, require_steerable=True)
    if queued:
        return {"queued": True, "session_id": session_id}
    # Refused — distinguish blank content from a non-steerable (best-of-N) run.
    if not (body.content or "").strip():
        return {"queued": False, "session_id": session_id, "reason": "empty content"}
    return {"queued": False, "unsupported": True, "session_id": session_id,
            "reason": "steering not available for this run"}


class _ApproveBody(BaseModel):
    decision: str = Field(..., description="'approve' | 'reject'")
    id: int | None = Field(None, description="approval seq id echoed from the event")
    note: str | None = None


@router.post("/api/chat/sessions/{session_id}/approve")
def chat_session_approve(session_id: int, body: _ApproveBody) -> dict:
    """Resolve a pending approval gate (#1) — the chat run is blocked
    waiting for the user's Approve/Reject on a risky/ask-policy action."""
    from aiforge_core.runtime import chat_approve
    ok = chat_approve.resolve(session_id, body.decision, body.note or "", body.id)
    return {"resolved": ok, "decision": body.decision, "session_id": session_id}


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
