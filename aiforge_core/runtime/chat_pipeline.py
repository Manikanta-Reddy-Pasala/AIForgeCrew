"""Conversational driver over the full ADK 2.x agent team.

Chat (no tickets) runs the same multi-agent pipeline the tickets use
(``pipeline.build_pipeline`` → triage → planner → verifier → doer →
feedback → learner), ticketless, in the session's working dir, and
streams each agent's output/tool-calls back as conversational events.
Triage's fast-path keeps trivial messages cheap.

``stream_chat_pipeline(prompt, cwd)`` yields SSE-ready dicts:
``{"type":"agent","role","text"}`` · ``{"type":"tool","role","name","args"}``
· ``{"type":"tool_result","role","name","result"}`` ·
``{"type":"message","text"}`` (final) · ``{"type":"error","text"}`` ·
``{"type":"done"}``.

Falls back to the lightweight ReAct agent if ADK is unavailable or the
run errors, so chat never hard-breaks.
"""
from __future__ import annotations

import os
import queue
import threading
import time  # noqa: F401  # tests patch time through this module
from collections.abc import Generator

from .chat_pipeline_events import (  # noqa: F401  # re-exported
    _enhancer_block_reason,
    _event_text,
    _fold_team_event,
    _guard_edit_claim,
    _part_events,
    _planner_subtask_event,
    _process_team_event,
    _team_change_events,
    _team_streaming,
    map_event,
    partial_events,
)
from .chat_pipeline_prompt import (  # noqa: F401  # re-exported
    _build_team_prompt,
    _history_preamble,
)
from .chat_pipeline_turn import (  # noqa: F401  # re-exported
    _HANDED_OFF,
    _SENTINEL,
    _answer_ready,
    _bind_team_session,
    _close_turn,
    _compute_team_answer,
    _dur,
    _finalize_subtasks,
    _hand_off_turn,
    _persist_fallback_turn,
    _persist_stop_before_start,
    _promote_team_answer,
    _run_async_in_thread,
    _run_pipeline_fallback,
    _tail_team_queue,
    _team_deadline_s,
    _team_final_state,
    _team_plugins,
)

# Team runs mutate the process-global ``AIFORGE_REPO_ROOT`` (read by the
# sandbox + git tools). Two concurrent team chats would interleave that env
# and cross-contaminate cwd. Serialize team runs in-process so only one owns
# the env at a time. (Ticket runs execute in a separate runner process, so
# they don't share this lock.)
_RUN_LOCK = threading.Lock()
# Owner generation for the team lock. A holder records the generation it
# acquired under; a force-release (kill-all) bumps it, which invalidates the
# wedged holder so its finally does NOT release the lock again (it would free a
# NEW holder's lock — a plain Lock is unowned) nor restore AIFORGE_REPO_ROOT
# over the new run. Guarded by its own tiny lock so reads/writes are atomic.
_RUN_LOCK_GEN = 0
_RUN_LOCK_GEN_LOCK = threading.Lock()


def _run_lock_gen() -> int:
    with _RUN_LOCK_GEN_LOCK:
        return _RUN_LOCK_GEN


def force_release_run_lock() -> bool:
    """Escape hatch: drop the team run-serialization lock even if another thread
    holds it. Used by the chat 'reset / kill all' control to recover when a team
    run wedged (e.g. blocked in an LLM call that outlives a Stop) and left the
    lock held, so a new chat sits forever on 'waiting for another team run'.

    Bumps the owner generation so the wedged holder's finally becomes a no-op
    (it won't double-release the lock onto a new holder, nor restore the env
    root over a new run). Safe because kill-all also cancels every run, so the
    wedged holder is being torn down anyway.

    The gen-bump AND the lock release happen UNDER ``_RUN_LOCK_GEN_LOCK`` — the
    same lock the holder's teardown takes — so the two are mutually exclusive.
    Without that, a holder could pass its gen-check, get pre-empted before its
    release, and then release a NEW holder's lock + clobber its env root."""
    global _RUN_LOCK_GEN
    with _RUN_LOCK_GEN_LOCK:
        if not _RUN_LOCK.locked():
            return False
        _RUN_LOCK_GEN += 1              # invalidate the current holder
        try:
            _RUN_LOCK.release()
            return True
        except RuntimeError:
            return False


def _release_run_lock(my_lock_gen, prev_root) -> None:
    """Restore ``prev_root`` and release the run lock — but ONLY when this holder
    still owns it. If a kill-all force-released the lock (bumping the generation)
    another run now owns the lock + env root, so we must NOT release again or
    clobber their prev_root. The gen-check + restore + release run together under
    _RUN_LOCK_GEN_LOCK (the same lock force_release takes) so the check can't go
    stale before the release (TOCTOU)."""
    with _RUN_LOCK_GEN_LOCK:
        if _RUN_LOCK_GEN != my_lock_gen:
            return
        if prev_root is None:
            os.environ.pop("AIFORGE_REPO_ROOT", None)
        else:
            os.environ["AIFORGE_REPO_ROOT"] = prev_root
        try:
            _RUN_LOCK.release()
        except RuntimeError:
            pass


def _drive_teardown(root_token, my_lock_gen, prev_root, session_id, cwd,
                    raw_prompt, final_text, steps, sub_items, run_ok,
                    started_at, q, handed_off=False) -> None:
    """The team-run finally: reset the repo-root contextvar, release the run
    lock, reconcile the subtask panel to the outcome, persist the turn and clear
    the session's approver/cancel/steer state. Persistence is done HERE (the
    background thread), not the SSE generator, so a client disconnect can't drop
    the real answer or persist a partial one.

    A run that ``handed_off`` closed its turn when it posted the answer
    (:func:`_hand_off_turn`). A follow-up turn may own the session by now, so
    only the lock and the contextvar are left for this run to release."""
    if root_token is not None:
        from aiforge_core.runtime import request_context
        request_context.reset_repo_root(root_token)
    _release_run_lock(my_lock_gen, prev_root)
    if not handed_off:
        _close_turn(session_id, cwd, raw_prompt, final_text, steps, sub_items,
                    run_ok, started_at, q)
        if session_id is not None:
            # END THE RUN HERE, last. The SSE producer deliberately leaves it
            # open for a team turn (this driver owns the run's lifetime, the
            # same way it owns persistence), so this is what wakes every
            # subscriber and tells the idle compactor the box is free again.
            # Finishing it in the producer marked the run done the moment the
            # driver was launched: minutes of team work then looked like an
            # idle box, and memory compaction folded briefs in the middle of a
            # run that was still calling tools.
            from aiforge_core.runtime import chat_runs
            chat_runs.finish(session_id)
    q.put(_SENTINEL)


async def _close_team_run(agen, runner) -> None:
    """ADK-native stop: aclose() the run generator (cancels the in-flight agent +
    all its sub-agents) and close the runner. Both best-effort."""
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass
    try:
        await runner.close()
    except Exception:  # noqa: BLE001
        pass


def _emit_steer_acks(session_id, chat_interject, q) -> None:
    """Surface a "📌 Got your message" ack for any queued steer (Gap A): the
    Doer/Refiner's before_model callback already folded it into its next model
    call — this just mirrors the ack the simple loop shows, polled once per
    event since the callback has no direct handle to this queue."""
    if session_id is None:
        return
    from aiforge_core.runtime import chat_steer
    for applied in chat_interject.pop_applied(session_id):
        q.put(chat_steer.applied_event(applied))


async def _drive_run_events(agen, runner, q, session_id, chat_interject,
                            steps: list, on_answer=None) -> dict:
    """Drive the team pipeline's event stream: surface steer acks, honour Stop,
    map each ADK event to the queue + accumulators, and stop on the Enhancer's
    too-vague sentinel. Returns ``{by_role, final, sub_items, enhancer_blocked,
    answered}`` (``steps`` is mutated in place).

    ``on_answer`` is awaited once, with the accumulators so far, when the answer
    is final (:func:`_answer_ready`), before the Learner runs. From then on the
    session belongs to the next turn: its steers and its Stop are not this
    run's, and the Learner runs to completion."""
    from aiforge_core.runtime import chat_cancel
    by_role: dict[str, str] = {}
    final = ""
    acc = {"emitted_subtasks": False, "sub_items": None}
    enhancer_blocked = None
    answered = False
    async for event in agen:
        if not answered:
            _emit_steer_acks(session_id, chat_interject, q)
            if session_id is not None and chat_cancel.is_cancelled(session_id):
                await _close_team_run(agen, runner)
                q.put({"type": "error", "text": "stopped by user", "stopped": True})
                break
            if on_answer is not None and _answer_ready(event):
                answered = True
                await on_answer({"by_role": by_role, "final": final,
                                 "sub_items": acc["sub_items"],
                                 "enhancer_blocked": enhancer_blocked})
        if getattr(event, "partial", False):
            # A streamed chunk: shown live as the agent's draft line. The full
            # (non-partial) event that follows carries the same text and is
            # what becomes the step, the answer and the accumulators.
            for ev in partial_events(event):
                q.put(ev)
            continue
        enhancer_blocked = _fold_team_event(event, q, steps, by_role, acc) \
            or enhancer_blocked
        if enhancer_blocked:
            await _close_team_run(agen, runner)
            break
        t = _event_text(event)
        if t:
            final = t
    return {"by_role": by_role, "final": final, "sub_items": acc["sub_items"],
            "enhancer_blocked": enhancer_blocked, "answered": answered}


def _acquire_team_run_lock(session_id, cwd, raw_prompt, started_at, q):
    """Acquire the process-wide team-run lock, cancellably. Returns the owner
    lock-generation on success (a kill-all force-release bumps it, which lets a
    holder neutralise its own teardown), or None when Stop landed while waiting —
    in which case the stop events + sentinel are already on the queue and the
    caller returns immediately."""
    from aiforge_core.runtime import chat_cancel
    waited = False
    while True:
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            if session_id is not None:
                _persist_stop_before_start(session_id, cwd, raw_prompt, started_at)
            q.put({"type": "error", "text": "stopped by user", "stopped": True})
            q.put(_SENTINEL)
            return None
        if _RUN_LOCK.acquire(timeout=0.5):
            return _run_lock_gen()
        if not waited:
            waited = True
            q.put({"type": "thought", "role": "system",
                   "text": "waiting for another team run to finish…"})


async def _drive(q, session_id, cwd, raw_prompt, started_at, prompt, _team_state):
    _bind_team_session(session_id, q)
    # Serialize the AIFORGE_REPO_ROOT mutation across concurrent team runs,
    # cancellably + with feedback so a 2nd concurrent run doesn't stall its
    # client silently behind a long-running first run.
    my_lock_gen = _acquire_team_run_lock(session_id, cwd, raw_prompt,
                                         started_at, q)
    if my_lock_gen is None:
        return                       # stopped while waiting — already handled
    from aiforge_core.runtime import chat_interject
    # Lock is held — everything from here is inside try/finally so the
    # env mutation can't leak the lock if it raises.
    prev_root = os.environ.get("AIFORGE_REPO_ROOT")
    # Request-scoped repo root: the contextvar isolates concurrent chats on
    # different repos (the env below is process-global and clobbers). The
    # contextvar propagates into the ADK run (same async task/thread) and
    # into asyncio.to_thread tool dispatch (which copies the context); the
    # os.environ set is kept for the subprocess graph-runner path + as a
    # cross-thread fallback for any executor that doesn't copy context.
    root_token = None
    steps: list[dict] = []
    final_text = ""
    # Subtask panel tracking: the Planner emits a plan (all pending); the
    # Doer then executes it monolithically, so we don't get a per-subtask
    # signal. We reconcile the panel to the RUN OUTCOME at the end (done on
    # success, failed on error/stop) — otherwise the panel is frozen at
    # "0/N pending" even after the run reports complete.
    _sub_items: list[dict] | None = None
    _run_ok = False
    _handed_off = False                  # answer posted, Learner still running
    _run_id = None                       # keys this run's shell/browser/kernel
    try:
        os.environ["AIFORGE_REPO_ROOT"] = cwd
        from aiforge_core.runtime import request_context
        root_token = request_context.set_repo_root(cwd)
        # Blocking first-time codegraph build for the pipeline's repo so the
        # Doer's codegraph tools are available (a fresh repo has no index →
        # tools silently dropped). Best-effort; never blocks the run on it.
        try:
            from aiforge_core.runtime.tools import codegraph as _cg
            _cg.ensure_indexed(cwd)
        except Exception:  # noqa: BLE001
            pass
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as gtypes

        from .pipeline import build_pipeline

        # Baseline commit so we can show a Changes diff after a SEQUENTIAL
        # team run too (the parallel path already emits one; this path never
        # did). git-init + committed baseline makes HEAD a valid diff base
        # even in a fresh, non-repo chat workspace.
        _seq_start_sha = ""
        try:
            from .parallel_subtasks import _commit_turn_baseline
            _seq_start_sha = _commit_turn_baseline(cwd)
        except Exception:  # noqa: BLE001
            _seq_start_sha = ""

        # Full context by default — the Researcher + context gatherers feed
        # the Planner so it decomposes into well-scoped subtasks (this IS
        # useful, especially for splitting). Opt into a LEAN run with
        # AIFORGE_CHAT_LEAN=1 when you want the Planner/subtasks fast on a
        # slow local model (skips researcher + ctx_conventions; the Doer
        # still has grep/read to pull repo context on demand).
        _lean = os.environ.get("AIFORGE_CHAT_LEAN", "0") in ("1", "true")
        pipeline = build_pipeline(
            project=None,
            skip_researcher=_lean,
            skip_conventions=_lean,
            skip_repomap=_lean,   # the repomap agent can runaway-loop on an
                                  # empty chat workspace; lean skips it so the
                                  # Planner (and subtask decomposition) runs.
        )
        svc = InMemorySessionService()
        # Phantom-tool guard: a text-only agent (feedback/validator/learner)
        # can emit a hallucinated function_call; without this ADK raises
        # "Tool X not found" and the whole SequentialAgent pipeline aborts
        # mid-flight. The plugin turns it into a graceful observation so the
        # run survives to its answer.
        _plugins = _team_plugins()
        runner = Runner(agent=pipeline, app_name="aiforge-chat",
                        session_service=svc, auto_create_session=True,
                        plugins=_plugins)
        session = await svc.create_session(
            app_name="aiforge-chat", user_id="chat",
            state=_team_state,
        )
        # One shell (tmux), browser and kernel for THIS run. Unkeyed, every
        # team run shared the "default" shell: a run on repo B ran its
        # commands in the directory repo A's run had cd'd into.
        from .run_resources import key_stateful_tools
        _run_id = session.id
        key_stateful_tools(_run_id)
        content = gtypes.Content(
            role="user", parts=[gtypes.Part.from_text(text=prompt)])
        kw = {"user_id": "chat", "session_id": session.id, "new_message": content}
        try:
            from google.adk.agents.run_config import RunConfig
            # High cap — a real multi-agent build legitimately needs
            # many calls; the repeat_guard stops genuine stuck loops, so
            # we don't rely on a low ceiling. Tune AIFORGE_CHAT_MAX_LLM_CALLS.
            kw["run_config"] = RunConfig(
                max_llm_calls=int(os.environ.get("AIFORGE_CHAT_MAX_LLM_CALLS", "600")),
                **_team_streaming())
        except Exception:
            pass
        agen = runner.run_async(**kw)

        async def _answer_now(res) -> None:
            # The validator passed: post the answer now and let the Learner
            # (an LLM call + its memory writes) finish behind it.
            nonlocal final_text, _run_ok, _sub_items, _handed_off
            msg, change_events = await _compute_team_answer(
                svc, session, res["by_role"], res["final"],
                res["enhancer_blocked"], cwd, _seq_start_sha)
            if res["sub_items"] is not None:
                _sub_items = res["sub_items"]
            final_text, _run_ok = msg, True
            q.put({"type": "message", "text": msg})
            for _ev in change_events:
                q.put(_ev)
            _hand_off_turn(q, session_id, cwd, raw_prompt, final_text, steps,
                           _sub_items, started_at)
            _handed_off = True

        evres = await _events_under_deadline(agen, runner, q, session_id,
                                             chat_interject, steps, _answer_now)
        if evres is None or _handed_off:  # deadline (reported) / answered
            return
        by_role, final = evres["by_role"], evres["final"]
        _sub_items = evres["sub_items"] if evres["sub_items"] is not None else _sub_items
        _enhancer_blocked_reason = evres["enhancer_blocked"]
        msg, _change_events = await _compute_team_answer(
            svc, session, by_role, final, _enhancer_blocked_reason,
            cwd, _seq_start_sha)
        final_text = msg
        _run_ok = True
        q.put({"type": "message", "text": msg})
        for _ev in _change_events:
            q.put(_ev)
    except Exception as exc:  # noqa: BLE001
        q.put({"type": "error", "text": f"pipeline: {exc}"})
        # The turn ended with no answer, and whatever the run had already
        # written is on disk. Same structural marker a Stop leaves, for the
        # same reason: without it `chat_resume` reads this as a turn that
        # finished normally, and Retry re-runs the whole pipeline from
        # nothing — re-doing every edit the dead run made. Team mode is the
        # expensive path to repeat.
        q.put({"type": "stopped", "reason": "pipeline_error"})
    finally:
        if _run_id is not None:
            from .run_resources import destroy_run_resources
            destroy_run_resources(_run_id)
        _drive_teardown(root_token, my_lock_gen, prev_root, session_id, cwd,
                        raw_prompt, final_text, steps, _sub_items, _run_ok,
                        started_at, q, _handed_off)


async def _events_under_deadline(agen, runner, q, session_id, chat_interject,
                                 steps, on_answer=None):
    """``_drive_run_events`` bounded by :func:`_team_deadline_s`. Returns its
    result, or None after reporting a deadline stop (same structural marker a
    user Stop leaves, so Retry resumes instead of redoing the whole run)."""
    import asyncio
    import contextlib
    deadline = _team_deadline_s()
    cm = (asyncio.timeout(deadline) if deadline and deadline > 0
          else contextlib.nullcontext())
    try:
        async with cm:
            return await _drive_run_events(agen, runner, q, session_id,
                                           chat_interject, steps, on_answer)
    except TimeoutError:
        with contextlib.suppress(Exception):
            await agen.aclose()
        q.put({"type": "error",
               "text": (f"team run stopped at its {int(deadline // 60)}-minute "
                        "deadline (AIFORGE_CHAT_TEAM_DEADLINE_S)")})
        q.put({"type": "stopped", "reason": "deadline"})
        return None



def _drive_awake(q, session_id, cwd, raw_prompt, started_at, prompt, _team_state):
    # A team run is minutes of work. Locking the screen and walking away
    # used to let the box idle into sleep mid-run, which suspends the whole
    # process: the model socket dies and everything already done waits to
    # be re-done. The assertion lives in a child process, so it goes away
    # with this run even if the API is killed outright.
    from aiforge_core.runtime.keep_awake import keep_awake
    with keep_awake(f"team run session={session_id}"):
        _run_async_in_thread(lambda: _drive(q, session_id, cwd, raw_prompt, started_at, prompt, _team_state))



def stream_chat_pipeline(prompt: str, *, cwd: str,
                         session_id: int | None = None,
                         history: list[dict] | None = None,
                         started_at: float | None = None,
                         resume_brief: str = "") -> Generator[dict, None, bool]:
    """Yield the team turn's events, ending with ``done``. Returns True when the
    driver handed the turn off (answer posted and persisted, Learner still
    running); the caller then finishes the chat run itself."""
    q: queue.Queue = queue.Queue()
    from aiforge_core.runtime import chat_cancel
    raw_prompt = prompt   # the user's actual request (before context augmentation)
    prompt, _team_state = _build_team_prompt(cwd, prompt, history, session_id,
                                             resume_brief)

    t = threading.Thread(target=lambda: _drive_awake(q, session_id, cwd, raw_prompt, started_at, prompt, _team_state), daemon=True)
    t.start()
    flags = {"errored": False, "stopped": False, "saw_real": False}
    yield from _tail_team_queue(q, flags)
    # Fall back to the lightweight agent ONLY when the pipeline couldn't run at
    # all — it errored, produced NO substantive events, and the user didn't Stop
    # it. (A user Stop, or an error mid-run after real output, must NOT silently
    # launch a second agent.)
    if flags["errored"] and not flags["saw_real"] and not flags["stopped"]:
        yield from _run_pipeline_fallback(raw_prompt, cwd, session_id, started_at)
    yield {"type": "done"}
    return bool(flags.get("handed_off"))
