"""``spawn_task``: hand an independent part of the request to a second agent.

Default is **wait=true**: the child runs nested in this turn (same process,
sequential model calls — no second session fighting for a single slot) and its
FINAL answer is the tool result. That matches Claude Code / Cursor / OpenCode
Task tools.

``wait=false`` keeps the older fire-and-forget side chat: a child session runs
alongside, and its answer is posted when it finishes.
"""
from __future__ import annotations

import os
from contextvars import ContextVar

_SPAWN_DEPTH: ContextVar[int] = ContextVar("aiforge_spawn_depth", default=0)
_MAX_NEST = 1


def _truthy(val, *, default: bool) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on", "wait")


def _t_spawn_task(args: dict, cwd: str) -> dict:
    task = str(args.get("task") or "").strip()
    if len(task) < 12:
        return {"ok": False, "error": "spawn_task needs the full task text "
                "(what to do, with the paths and names it needs)"}
    mode = str(args.get("mode") or "simple").lower()
    if mode not in ("simple", "plan"):
        mode = "simple"
    wait = _truthy(args.get("wait"), default=True)
    if wait:
        return _wait_nested(task, mode, cwd)
    return _fire_side(task, mode)


def _fire_side(task: str, mode: str) -> dict:
    """Legacy parallel path: child chat, answer posted later."""
    try:
        from aiforge_core.runtime import chat_store, request_context
        raw = request_context.get_session_id()
        sid = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        sid = 0
    if not sid:
        return {"ok": False, "error": "no chat session: do this part yourself"}
    try:
        session = chat_store.get_session(sid) or {}
        if session.get("parent_id"):
            return {"ok": False, "error": "this is already a side task: do "
                    "the work yourself instead of starting more"}
        from aiforge_core.api.routes._chat import _side_tasks
        child = _side_tasks.create(sid, task, mode)
    except Exception as exc:  # noqa: BLE001 — a failed spawn means: do it yourself
        return {"ok": False, "error": f"could not start it ({exc}): do this part yourself"}
    return {"ok": True, "task_id": child.get("id"),
            "state": (child.get("task") or {}).get("state") or child.get("state"),
            "waited": False,
            "note": "it runs in its own chat and its answer is posted here when "
                    "it finishes. Do not wait for it: continue with your own "
                    "part of the request."}


def _wait_nested(task: str, mode: str, cwd: str) -> dict:
    """Run a nested chat agent to completion; return its FINAL as the answer."""
    depth = _SPAWN_DEPTH.get()
    if depth >= _MAX_NEST:
        return {"ok": False, "error": "already inside a spawned agent: do "
                "this part yourself instead of nesting further"}
    try:
        max_steps = int(os.environ.get("AIFORGE_SPAWN_WAIT_STEPS", "24"))
    except ValueError:
        max_steps = 24
    max_steps = max(4, min(max_steps, 60))
    token = _SPAWN_DEPTH.set(depth + 1)
    try:
        from aiforge_core.runtime.chat_agent import run_chat_agent
        answer = ""
        error = ""
        # plan = read-only; simple/act may edit — same as the side-task modes.
        agent_mode = "plan" if mode == "plan" else "act"
        for ev in run_chat_agent(
                [{"role": "user", "content": (
                    "You are a SIDE AGENT answering one delegated part of a "
                    "larger request. Do ONLY what is asked below, then FINAL "
                    "with a complete answer the parent agent can use.\n\n"
                    + task)}],
                cwd=cwd or ".",
                role="chat",
                mode=agent_mode,
                max_steps=max_steps,
                session_id=None,
                carry=False,
                strict_finish=False):
            kind = (ev or {}).get("type")
            if kind == "message":
                answer = (ev.get("text") or answer or "").strip()
            elif kind == "error":
                error = (ev.get("text") or error or "").strip()
        if answer:
            return {"ok": True, "answer": answer[:12000], "waited": True,
                    "mode": mode}
        if error:
            return {"ok": False, "error": error[:500], "waited": True}
        return {"ok": False, "error": "spawned agent finished with no answer",
                "waited": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"spawn wait failed ({exc}): do this "
                "part yourself", "waited": True}
    finally:
        _SPAWN_DEPTH.reset(token)


__all__ = ["_t_spawn_task"]
