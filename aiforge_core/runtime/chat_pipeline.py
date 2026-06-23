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
from collections.abc import Callable, Iterator

_SENTINEL = object()

# Team runs mutate the process-global ``AIFORGE_REPO_ROOT`` (read by the
# sandbox + git tools). Two concurrent team chats would interleave that env
# and cross-contaminate cwd. Serialize team runs in-process so only one owns
# the env at a time. (Ticket runs execute in a separate runner process, so
# they don't share this lock.)
_RUN_LOCK = threading.Lock()


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


def stream_chat_pipeline(prompt: str, *, cwd: str,
                         session_id: int | None = None,
                         history: list[dict] | None = None) -> Iterator[dict]:
    q: queue.Queue = queue.Queue()
    from aiforge_core.runtime import chat_cancel
    raw_prompt = prompt   # the user's actual request (before context augmentation)
    # Build a context-rich prompt: project summary + prior conversation +
    # the current request, so the team pipeline isn't clueless on follow-ups.
    try:
        from aiforge_core.runtime.chat_agent import (
            _memory_recall, _repo_context, _rules_context,
        )
        rules_ctx = _rules_context(cwd)
        repo_ctx = _repo_context(cwd)
    except Exception:  # noqa: BLE001
        rules_ctx = repo_ctx = ""
        _memory_recall = None  # type: ignore
    convo = _history_preamble(history)
    # SESSION START (self-learning): on a fresh session (no prior turns)
    # recall memory keyed to the opening request so the team arrives
    # informed by earlier sessions, same as the lightweight agent.
    recall_ctx = ""
    is_init = not convo
    if is_init and _memory_recall is not None:
        try:
            recall_ctx = _memory_recall(cwd, prompt)
        except Exception:  # noqa: BLE001
            recall_ctx = ""
    parts = [p for p in (rules_ctx, repo_ctx, recall_ctx, convo) if p]
    prompt = ("\n\n".join(parts) + f"\n\nCURRENT REQUEST:\n{prompt}"
              if parts else prompt)

    async def _drive() -> None:
        # Bind this driver thread (+ the bash tool the Doer runs) to the
        # session so the Stop button can cancel + kill its subprocesses.
        chat_cancel.set_active(session_id)
        # Attach an interactive approver so the Doer's tool gate can pause
        # this team run for human Approve/Reject (the gate no-ops without it).
        if session_id is not None:
            from aiforge_core.runtime import chat_approve
            chat_approve.set_emitter(session_id, q.put)
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
                    chat_cancel.finish(session_id)
                q.put({"type": "error", "text": "stopped by user", "stopped": True})
                q.put(_SENTINEL)
                return
            acquired = _RUN_LOCK.acquire(timeout=0.5)
            if not acquired and not waited:
                waited = True
                q.put({"type": "thought", "role": "system",
                       "text": "waiting for another team run to finish…"})
        # Lock is held — everything from here is inside try/finally so the
        # env mutation can't leak the lock if it raises.
        prev_root = os.environ.get("AIFORGE_REPO_ROOT")
        steps: list[dict] = []
        final_text = ""
        try:
            os.environ["AIFORGE_REPO_ROOT"] = cwd
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.genai import types as gtypes

            from .pipeline import build_pipeline

            pipeline = build_pipeline(project=None)
            svc = InMemorySessionService()
            runner = Runner(agent=pipeline, app_name="aiforge-chat",
                            session_service=svc, auto_create_session=True)
            session = await svc.create_session(
                app_name="aiforge-chat", user_id="chat",
                state={"chat_cwd": cwd},
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
            agen = runner.run_async(**kw)
            async for event in agen:
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
                    q.put(ev)
                    if ev.get("type") in ("thought", "tool", "error"):
                        steps.append(ev)
                # Track the latest substantive text — the last agent's
                # output is the conversational answer.
                t = _event_text(event)
                if t:
                    final = t
            try:
                sess = await svc.get_session(
                    app_name="aiforge-chat", user_id="chat", session_id=session.id)
                st = dict(sess.state or {})
            except Exception:
                st = {}
            msg = (final or st.get("doer_summary") or st.get("validator_summary")
                   or "Done.")
            final_text = msg
            q.put({"type": "message", "text": msg})
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "text": f"pipeline: {exc}"})
        finally:
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
            if session_id is not None:
                try:
                    from aiforge_core.runtime import chat_persist
                    chat_persist.persist_turn(
                        session_id=session_id, cwd=cwd, prompt=raw_prompt,
                        final_text=final_text, steps=steps, team=True,
                        cancelled=cancelled, awaiting=False)
                except Exception:  # noqa: BLE001
                    pass
            if session_id is not None:
                from aiforge_core.runtime import chat_approve
                chat_approve.clear_emitter(session_id)
                chat_approve.finish(session_id)
                chat_cancel.finish(session_id)
            q.put(_SENTINEL)

    t = threading.Thread(target=lambda: _run_async_in_thread(_drive), daemon=True)
    t.start()
    errored = False
    stopped = False
    saw_real = False     # any substantive (non-error) event from the pipeline
    while True:
        item = q.get()
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
                    cancelled=cancelled_fb, awaiting=False)
                _cc.finish(session_id)
        except Exception:
            pass
    yield {"type": "done"}
