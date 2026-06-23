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
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro_factory())
    finally:
        try:
            loop.close()
        except Exception:
            pass


def stream_chat_pipeline(prompt: str, *, cwd: str,
                         session_id: int | None = None) -> Iterator[dict]:
    q: queue.Queue = queue.Queue()
    from aiforge_core.runtime import chat_cancel

    async def _drive() -> None:
        # Bind this driver thread (+ the bash tool the Doer runs) to the
        # session so the Stop button can cancel + kill its subprocesses.
        chat_cancel.set_active(session_id)
        prev_root = os.environ.get("AIFORGE_REPO_ROOT")
        os.environ["AIFORGE_REPO_ROOT"] = cwd
        try:
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
                kw["run_config"] = RunConfig(max_llm_calls=int(
                    os.environ.get("AIFORGE_CHAT_MAX_LLM_CALLS", "120")))
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
                    q.put({"type": "error", "text": "stopped by user"})
                    break
                for ev in map_event(event):
                    q.put(ev)
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
            q.put({"type": "message", "text": msg})
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "text": f"pipeline: {exc}"})
        finally:
            if prev_root is None:
                os.environ.pop("AIFORGE_REPO_ROOT", None)
            else:
                os.environ["AIFORGE_REPO_ROOT"] = prev_root
            q.put(_SENTINEL)

    t = threading.Thread(target=lambda: _run_async_in_thread(_drive), daemon=True)
    t.start()
    errored = False
    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        if item.get("type") == "error":
            errored = True
        yield item
    # Fallback to the lightweight agent if the pipeline couldn't run at all.
    if errored:
        try:
            from .chat_agent import run_chat_agent
            yield {"type": "agent", "role": "fallback",
                   "text": "(pipeline unavailable — using the lightweight agent)"}
            for ev in run_chat_agent([{"role": "user", "content": prompt}], cwd=cwd):
                if ev.get("type") != "done":
                    yield ev
        except Exception:
            pass
    yield {"type": "done"}
