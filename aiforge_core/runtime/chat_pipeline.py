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
import time


def _dur(started_at: "float | None") -> "float | None":
    """Per-turn wall-clock seconds since ``started_at`` (None → unknown)."""
    return round(time.time() - started_at, 2) if started_at else None
from collections.abc import Callable, Iterator

_SENTINEL = object()

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


def _part_events(author: str, part) -> list[dict]:
    """Map a content part to the chat's existing event vocabulary
    (thought / tool) so no frontend change is needed. Each agent's
    interim text streams as a role-labelled 'thought'; the final answer
    is emitted separately as 'message' by the driver."""
    out: list[dict] = []
    text = getattr(part, "text", None)
    if text and text.strip():
        # `role` = the agent (author) so the UI can badge each step with
        # WHICH agent produced it. Text kept clean (no inline **author**).
        out.append({"type": "thought", "role": author, "text": text.strip()})
    fc = getattr(part, "function_call", None)
    if fc is not None:
        out.append({"type": "tool", "role": author,
                    "name": getattr(fc, "name", "?"),
                    "args": dict(getattr(fc, "args", None) or {}),
                    "result": {"by": author}})
    fr = getattr(part, "function_response", None)
    if fr is not None:
        resp = getattr(fr, "response", None)
        summary = resp if isinstance(resp, str) else (
            str(resp)[:200] if resp is not None else "")
        out.append({"type": "thought", "role": author,
                    "text": f"{getattr(fr, 'name', '?')} → {summary}"})
    return out


def map_event(event) -> list[dict]:
    """Map one ADK event to conversational dicts. Pure — unit-testable."""
    author = getattr(event, "author", None) or "agent"
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    out: list[dict] = []
    for p in parts:
        out.extend(_part_events(author, p))
    return out


def _event_text(event) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts).strip()


def _run_async_in_thread(coro_factory: Callable) -> None:
    import asyncio
    loop = asyncio.new_event_loop()

    def _quiet_handler(loop, context):  # noqa: ANN001
        # Swallow litellm LoggingWorker noise (CancelledError / TimeoutError
        # / "task was destroyed") that asyncio would otherwise print to
        # stderr when we tear the loop down. Surface anything else.
        msg = str(context.get("message", "")) + str(context.get("exception", ""))
        if "LoggingWorker" in msg or "logging_worker" in repr(context.get("future", "")):
            return
        exc = context.get("exception")
        if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
            return
        loop.default_exception_handler(context)

    try:
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_quiet_handler)
        loop.run_until_complete(coro_factory())
    finally:
        # Drain leftover background tasks (litellm's LoggingWorker etc.)
        # BEFORE closing — otherwise abruptly closing the loop cancels them
        # mid-flight and spams "Task exception was never retrieved" /
        # "task_done() called too many times".
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def _history_preamble(history: list[dict] | None) -> str:
    """Render prior turns so the team pipeline has conversation continuity
    (it starts a fresh ADK session per message and would otherwise be
    clueless on follow-ups). Drops the trailing current user message."""
    if not history:
        return ""
    prior = list(history)
    if prior and prior[-1].get("role") == "user":
        prior = prior[:-1]
    if not prior:
        return ""
    lines = []
    for m in prior[-12:]:
        who = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {(m.get('content') or '')[:800]}")
    return "CONVERSATION SO FAR (continue with this context):\n" + "\n".join(lines)


def _finalize_subtasks(items: list[dict] | None, run_ok: bool,
                       cancelled: bool) -> list[dict]:
    """Reconcile the Planner's subtask panel to the run outcome.

    The chat (sequential team) pipeline shows a Planner-decomposed task
    list but the Doer executes it in one pass — there's no per-subtask
    completion signal, so without this the panel sits at "0/N pending"
    after the run reports complete. Mutates each item's status in place
    (the same dicts are persisted in ``steps`` → reload agrees) and
    returns the matching ``subtask_update`` events to stream live.

    done on a clean finish; failed on error / user-stop.
    """
    if not items:
        return []
    status = "done" if (run_ok and not cancelled) else "failed"
    out: list[dict] = []
    for it in items:
        it["status"] = status
        out.append({"type": "subtask_update",
                    "slug": it.get("slug"), "status": status})
    return out


def stream_chat_pipeline(prompt: str, *, cwd: str,
                         session_id: int | None = None,
                         history: list[dict] | None = None,
                         started_at: float | None = None,
                         resume_brief: str = "") -> Iterator[dict]:
    q: queue.Queue = queue.Queue()
    from aiforge_core.runtime import chat_cancel
    raw_prompt = prompt   # the user's actual request (before context augmentation)
    # A resume brief is CONTEXT, not the request. Folding it into `prompt`
    # before this line would make it the "user's actual request": raw_prompt is
    # what gets persisted as the turn's request, written into long-term memory
    # (chat_persist: "**Request:** …"), and used as the RECALL QUERY for rules /
    # skills / memory. A short ask would be stored and retrieved as mostly
    # "[RESUME] Your previous attempt…" boilerplate. It joins the planner-facing
    # prompt below instead, with the context blocks.
    # Build a context-rich prompt: project summary + prior conversation +
    # the current request, so the team pipeline isn't clueless on follow-ups.
    # ONE shared context bundle — same source-selection/scoping/gating as single
    # chat (context_bundle.build_bundle), so team-chat can never silently miss a
    # source the single path injects.
    cave = False
    _ctx_on = lambda _b: True  # noqa: E731
    try:
        from aiforge_core.runtime.chat_agent import _cave_mode, _ctx_on
        cave = _cave_mode()
    except Exception:  # noqa: BLE001
        pass
    from aiforge_core.runtime import context_bundle as _cb
    bundle = _cb.build_bundle(cwd, raw_prompt, cave=cave, ctx_on=_ctx_on,
                              session_id=session_id, want_repo_map=False)
    convo = _history_preamble(history)
    # SESSION IMAGES — descriptions of attached images, queryable all session.
    img_ctx = ""
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_media
            img_ctx = chat_media.context_block(session_id)
        except Exception:  # noqa: BLE001
            img_ctx = ""
    parts = [p for p in (*bundle.blocks(), img_ctx, convo) if p]
    prompt = ("\n\n".join(parts) + f"\n\nCURRENT REQUEST:\n{prompt}"
              if parts else prompt)
    if resume_brief:
        prompt = f"{prompt}\n\n{resume_brief}"
    # ALSO expose these as pipeline STATE keys — many graph nodes run
    # include_contents='none' and read the {rules_md?}/{memory_brief_md?}/
    # {user_prefs_md?} placeholders, NOT the seed prose above.
    _team_state = {"chat_cwd": cwd}
    if bundle.rules_md:
        _team_state["rules_md"] = bundle.rules_md
    if bundle.memory_md:
        _team_state["memory_brief_md"] = bundle.memory_md
    if bundle.preferences_md:
        _team_state["user_prefs_md"] = bundle.preferences_md

    async def _drive() -> None:
        # Bind this driver thread (+ the bash tool the Doer runs) to the
        # session so the Stop button can cancel + kill its subprocesses.
        chat_cancel.set_active(session_id)
        # Attach an interactive approver so the Doer's tool gate can pause
        # this team run for human Approve/Reject (the gate no-ops without it).
        from aiforge_core.runtime import chat_interject
        if session_id is not None:
            from aiforge_core.runtime import chat_approve
            chat_approve.set_emitter(session_id, q.put)
            # Mark this team run STEERABLE so /steer accepts mid-run guidance —
            # the Doer/Refiner before_model callback (chat_steer_callback) folds
            # it in and this loop surfaces the "📌 Got your message" ack. Without
            # this, push(require_steerable=True) refused every steer and the UI
            # (wrongly) reported "steering not available in team mode".
            chat_interject.set_steerable(session_id, True)
            # Expose the session so the Doer's subtask_update tool can push a
            # LIVE subtask_update event onto this stream (real-time status in
            # the pinned dock), not just persist it to the ticket store.
            os.environ["AIFORGE_CURRENT_SESSION"] = str(session_id)
            # …and bind it to THIS thread's context, which is what the request
            # meter reads. The env var is deliberately not trusted there (it is
            # process-global and never cleared, so it bills a background
            # thread's work to whichever chat ran last), and the driver runs in
            # a bare Thread that inherits no context — so a team turn had no
            # session at all: no per-turn request count, no tokens, and the
            # chat footer's usage line suppressed entirely.
            from aiforge_core.runtime import request_context as _rc
            _rc.set_session_id(session_id)
        # Serialize the AIFORGE_REPO_ROOT mutation across concurrent team runs.
        # Acquire CANCELLABLY + with feedback so a 2nd concurrent team run
        # doesn't stall its client silently behind a long-running first run.
        acquired = False
        waited = False
        while not acquired:
            if session_id is not None and chat_cancel.is_cancelled(session_id):
                if session_id is not None:
                    from aiforge_core.runtime import chat_approve
                    chat_approve.clear_emitter(session_id)
                    chat_approve.finish(session_id)
                    # Persist a stopped turn here — the api _produce finally
                    # skips persistence for the team path (_path["driver"] is
                    # already set), so without this a Stop while waiting on the
                    # lock leaves the user msg with NO assistant turn on reload.
                    try:
                        from aiforge_core.runtime import chat_persist
                        chat_persist.persist_turn(
                            session_id=session_id, cwd=cwd, prompt=raw_prompt,
                            final_text="(stopped before the run started)",
                            steps=[], team=True, cancelled=True, awaiting=False,
                            mode="team", duration_s=_dur(started_at))
                    except Exception:  # noqa: BLE001
                        pass
                    chat_cancel.finish(session_id)
                q.put({"type": "error", "text": "stopped by user", "stopped": True})
                q.put(_SENTINEL)
                return
            acquired = _RUN_LOCK.acquire(timeout=0.5)
            if not acquired and not waited:
                waited = True
                q.put({"type": "thought", "role": "system",
                       "text": "waiting for another team run to finish…"})
        # Lock is held — record the owner generation so a kill-all force-release
        # (which bumps the generation) can neutralise this holder's teardown.
        my_lock_gen = _run_lock_gen()
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
            _plugins = []
            try:
                from .tool_error_plugin import PhantomToolGuardPlugin
                _plugins.append(PhantomToolGuardPlugin())
            except Exception:  # noqa: BLE001 — resilience is best-effort
                pass
            runner = Runner(agent=pipeline, app_name="aiforge-chat",
                            session_service=svc, auto_create_session=True,
                            plugins=_plugins)
            session = await svc.create_session(
                app_name="aiforge-chat", user_id="chat",
                state=_team_state,
            )
            content = gtypes.Content(
                role="user", parts=[gtypes.Part.from_text(text=prompt)])
            kw = dict(user_id="chat", session_id=session.id, new_message=content)
            try:
                from google.adk.agents.run_config import RunConfig
                # High cap — a real multi-agent build legitimately needs
                # many calls; the repeat_guard stops genuine stuck loops, so
                # we don't rely on a low ceiling. Tune AIFORGE_CHAT_MAX_LLM_CALLS.
                kw["run_config"] = RunConfig(max_llm_calls=int(
                    os.environ.get("AIFORGE_CHAT_MAX_LLM_CALLS", "600")))
            except Exception:
                pass
            final = ""
            by_role: dict[str, str] = {}
            emitted_subtasks = False
            _enhancer_blocked_reason = None
            agen = runner.run_async(**kw)
            async for event in agen:
                # Mid-run steering ack (Gap A, team mode): the Doer/Refiner's
                # before_model callback (chat_steer_callback) already folded
                # any queued steer into its next model call — this just
                # surfaces the same "📌 Got your message" acknowledgment the
                # simple-mode ReAct loop shows inline, polled once per event
                # since the callback has no direct handle to this queue.
                if session_id is not None:
                    from aiforge_core.runtime import chat_steer
                    for _applied in chat_interject.pop_applied(session_id):
                        q.put(chat_steer.applied_event(_applied))
                if session_id is not None and chat_cancel.is_cancelled(session_id):
                    # ADK-native stop: aclose() the run generator (cancels
                    # the in-flight agent + all its sub-agents) and close the
                    # runner, then kill any subprocess groups the Doer spawned.
                    try:
                        await agen.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await runner.close()
                    except Exception:  # noqa: BLE001
                        pass
                    q.put({"type": "error", "text": "stopped by user",
                           "stopped": True})
                    break
                for ev in map_event(event):
                    # The Enhancer's "too vague to act on" sentinel (see
                    # aiforge_core/runtime/prompts/enhancer.py — its stand-in
                    # for a clarifying question, since it must never ask one)
                    # must never reach the user as a raw thought bubble, and
                    # must stop the run here — same gap as the ticket path
                    # (adk_runner._enhancer_block_reason): without this, the
                    # sentinel silently became the Planner/Doer's brief and
                    # the run burned minutes building from garbage.
                    if (ev.get("type") == "thought" and ev.get("role") == "enhancer"
                            and (ev.get("text") or "").strip().startswith("ENHANCE_BLOCKED")):
                        _enhancer_blocked_reason = (
                            (ev.get("text") or "").strip().split(":", 1)[-1].strip()[:300]
                            or "the request is too vague to build a concrete plan from")
                        continue
                    q.put(ev)
                    if ev.get("type") in ("thought", "tool", "error"):
                        steps.append(ev)
                    # Track latest substantive text PER ROLE so the final
                    # answer can be the Doer's work — NOT the Learner's facts
                    # JSON, which runs last and would otherwise win.
                    if ev.get("type") == "thought" and ev.get("role") and ev.get("text"):
                        by_role[ev["role"]] = ev["text"]
                        # When the Planner decomposes a big task, surface the
                        # subtasks as a live task list in the chat UI (chat is
                        # ticketless, so this is ephemeral — the managed chart +
                        # parallel execution live on a ticket).
                        if ev["role"] == "planner" and not emitted_subtasks:
                            try:
                                from .subtasks_callback import _extract_subtickets
                                subs = _extract_subtickets(ev["text"])
                            except Exception:  # noqa: BLE001
                                subs = []
                            if subs:
                                emitted_subtasks = True
                                _sub_ev = {"type": "subtasks", "items": [
                                    {"slug": s.get("slug") or f"sub-{i+1}",
                                     "goal": s.get("goal") or s.get("title") or "",
                                     "status": "pending"}
                                    for i, s in enumerate(subs)]}
                                # Keep a handle so the finally block can reconcile
                                # these same item dicts to the run outcome (the
                                # SAME objects live in `steps`, so mutating them
                                # updates what gets persisted on reload).
                                _sub_items = _sub_ev["items"]
                                q.put(_sub_ev)
                                # Persist with the turn's steps so the subtask
                                # panel survives a navigate-away / reload.
                                steps.append(_sub_ev)
                if _enhancer_blocked_reason:
                    # Stop here — don't let the Planner/Doer run on a brief
                    # the Enhancer already flagged as too vague to act on.
                    try:
                        await agen.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await runner.close()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                t = _event_text(event)
                if t:
                    final = t
            try:
                sess = await svc.get_session(
                    app_name="aiforge-chat", user_id="chat", session_id=session.id)
                st = dict(sess.state or {})
            except Exception:
                st = {}
            # The Doer's output is the conversational answer (the actual work).
            # Learner/validator/refiner emit JSON verdicts, not user-facing
            # prose, and run AFTER the Doer — so never let them be the answer.
            # `doer_outcome` is the state key the Doer actually writes (both the
            # native agents/doer.py and the local text_doer FunctionNode). The
            # old `doer_summary`/`validator_summary` keys were never written, so
            # on a LOCAL endpoint — where the Doer is a text_doer FunctionNode
            # that emits no ADK "doer"-authored events — the answer fell through
            # to the Researcher's text or a bare "Done.", hiding the Doer's work.
            if _enhancer_blocked_reason:
                msg = (f"I need more detail before I can build this — "
                       f"{_enhancer_blocked_reason}. Could you say what to "
                       f"build/change and where?")
            else:
                msg = (by_role.get("doer") or st.get("doer_outcome")
                       or by_role.get("researcher") or final or "Done.")
            # Structured Changes diff (same PR-style view as the parallel path).
            # The sequential Doer edits the working tree, so include it. Compute
            # it BEFORE surfacing the answer so the claim-vs-reality guard can
            # cross-check an "applied fixes" claim against the ACTUAL diff — the
            # SAME events the UI renders, computed once (DRY).
            _change_events: list = []
            if _seq_start_sha and not _enhancer_blocked_reason:
                try:
                    from .parallel_subtasks import _emit_changes
                    _change_events = list(_emit_changes(
                        cwd, _seq_start_sha, include_worktree=True))
                except Exception:  # noqa: BLE001 — never break the turn
                    _change_events = []
            # The promoted answer (often the Enhancer's or a local text_doer's
            # prose) can claim it "applied fixes" while the diff is EMPTY — the
            # same hallucination the simple loop guards. When it asserts an edit
            # but nothing changed, prepend an honest note. A non-git run
            # (_seq_start_sha == "") gives no signal, so it's left alone.
            if _seq_start_sha and not _enhancer_blocked_reason and not _change_events:
                try:
                    from aiforge_core.runtime.chat_agent._context import (
                        _claims_file_edits, _edit_claim_disclaimer,
                        _edit_claim_guard_enabled)
                    if _edit_claim_guard_enabled() and _claims_file_edits(msg):
                        msg = _edit_claim_disclaimer(msg)
                except Exception:  # noqa: BLE001 — guard must never break a turn
                    pass
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
            # The repo-root contextvar is thread-local to THIS run, so reset it
            # unconditionally (no cross-run contamination like the shared env).
            if root_token is not None:
                from aiforge_core.runtime import request_context
                request_context.reset_repo_root(root_token)
            # If a kill-all force-released the lock out from under us, the
            # generation changed: another run (or none) now owns the lock + the
            # env root, so we must NOT release the lock again or restore our
            # prev_root over theirs. The gen-check + env-restore + release run
            # together under _RUN_LOCK_GEN_LOCK (the same lock force_release
            # takes) so the check can't go stale before the release (TOCTOU).
            with _RUN_LOCK_GEN_LOCK:
                if _RUN_LOCK_GEN == my_lock_gen:
                    if prev_root is None:
                        os.environ.pop("AIFORGE_REPO_ROOT", None)
                    else:
                        os.environ["AIFORGE_REPO_ROOT"] = prev_root
                    try:
                        _RUN_LOCK.release()
                    except RuntimeError:
                        pass
            # The team run owns BOTH the cancel-token lifetime AND persistence
            # — done HERE (background thread), not in the SSE generator, so a
            # client disconnect can't drop the real answer or persist a
            # partial one.
            cancelled = bool(session_id is not None
                             and chat_cancel.is_cancelled(session_id))
            # Reconcile the subtask panel to the outcome so it doesn't sit at
            # "0/N pending" after the run finishes. done on a clean finish,
            # failed on error/stop. Emit live updates AND mutate the persisted
            # item dicts (same objects in `steps`) so a reload shows the same.
            for _ev in _finalize_subtasks(_sub_items, _run_ok, cancelled):
                q.put(_ev)
            if session_id is not None:
                try:
                    from aiforge_core.runtime import chat_persist
                    chat_persist.persist_turn(
                        session_id=session_id, cwd=cwd, prompt=raw_prompt,
                        final_text=final_text, steps=steps, team=True,
                        cancelled=cancelled, awaiting=False,
                        mode="team", duration_s=_dur(started_at))
                except Exception:  # noqa: BLE001
                    pass
            if session_id is not None:
                from aiforge_core.runtime import chat_approve, chat_interject
                chat_approve.clear_emitter(session_id)
                chat_approve.finish(session_id)
                chat_cancel.finish(session_id)
                # Team mode does NOT fold steers in mid-run (see note below) —
                # but still clear so a queued steer can't leak into the next turn.
                chat_interject.clear(session_id)
            q.put(_SENTINEL)

    def _drive_awake() -> None:
        # A team run is minutes of work. Locking the screen and walking away
        # used to let the box idle into sleep mid-run, which suspends the whole
        # process: the model socket dies and everything already done waits to
        # be re-done. The assertion lives in a child process, so it goes away
        # with this run even if the API is killed outright.
        from aiforge_core.runtime.keep_awake import keep_awake
        with keep_awake(f"team run session={session_id}"):
            _run_async_in_thread(_drive)

    t = threading.Thread(target=_drive_awake, daemon=True)
    t.start()
    errored = False
    stopped = False
    saw_real = False     # any substantive (non-error) event from the pipeline
    while True:
        try:
            # Heartbeat: a slow local model can leave minute-long gaps between
            # agent steps. Without periodic output the SSE connection idles and
            # the browser/proxy drops it ("network error"). Emit a ping so the
            # stream stays warm; the UI ignores unknown event types.
            item = q.get(timeout=10)
        except queue.Empty:
            yield {"type": "ping"}
            continue
        if item is _SENTINEL:
            break
        if item.get("type") == "error":
            errored = True
            if item.get("stopped"):
                stopped = True
        else:
            saw_real = True
        yield item
    # Fall back to the lightweight agent ONLY when the pipeline couldn't run
    # at all — it errored, produced NO substantive events, and the user
    # didn't Stop it. (A user Stop, or an error mid-run after real output,
    # must NOT silently launch a second agent.)
    if errored and not saw_real and not stopped:
        try:
            from aiforge_core.runtime import chat_cancel as _cc
            from .chat_agent import run_chat_agent
            if session_id is not None:
                _cc.start(session_id)   # re-arm so Stop can halt the fallback
            yield {"type": "agent", "role": "fallback",
                   "text": "(pipeline unavailable — using the lightweight agent)"}
            fb_final = ""
            fb_steps: list[dict] = []
            for ev in run_chat_agent([{"role": "user", "content": raw_prompt}],
                                     cwd=cwd, session_id=session_id):
                if ev.get("type") == "message":
                    fb_final = ev.get("text", "")
                elif ev.get("type") in ("thought", "tool", "error"):
                    fb_steps.append(ev)
                if ev.get("type") != "done":
                    yield ev
            # The fallback agent doesn't persist itself — do it here so its
            # answer survives reload (team _gen skips persistence for team).
            if session_id is not None:
                from aiforge_core.runtime import chat_persist
                cancelled_fb = _cc.is_cancelled(session_id)
                chat_persist.persist_turn(
                    session_id=session_id, cwd=cwd, prompt=raw_prompt,
                    final_text=fb_final, steps=fb_steps, team=False,
                    cancelled=cancelled_fb, awaiting=False,
                    mode="team", duration_s=_dur(started_at))
                _cc.finish(session_id)
                # CC4 — also finish the approval gate; a fallback torn down
                # mid-approval would otherwise leak _PENDING/_REVIEW_EDITS for
                # this session into the next turn.
                from aiforge_core.runtime import chat_approve as _ca
                _ca.finish(session_id)
                from aiforge_core.runtime import chat_interject as _ci
                _ci.clear(session_id)
        except Exception:
            pass
    yield {"type": "done"}
