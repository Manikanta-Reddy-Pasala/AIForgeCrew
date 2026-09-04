"""Doer-callable agent delegation (sub #8).

``delegate_to_agent(role, prompt)`` boots a single-LlmAgent ADK runner
for ``role`` (researcher / planner / refiner / triage / verifier) and
returns its final state. Useful when the Doer is mid-edit and needs
fresh planning help or a context-gathering pass.

The delegate inherits its identity from ``agents.yaml`` — same model
allowlist, same context window, same memory scope as the production
pipeline uses. Wall-clock cap enforced via asyncio.

Soft-error contract; unknown roles return ``{ok: False,
error: "unknown_role"}``.

Depth cap (sub #16): each delegate runs with a request-scoped depth
counter (``request_context``, a contextvar — NOT env, so concurrent
delegation chains stay independent) incremented; calls beyond
``AIFORGE_DELEGATION_MAX_DEPTH`` (default 3) return ``{ok: False,
error: "delegation_depth_exceeded"}`` instead of spawning. Prevents runaway
recursive delegation if a model decides to always delegate.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from ._trace import emit

_DELEGABLE_ROLES = {"researcher", "planner", "refiner", "triage", "verifier"}
_MAX_DEPTH_ENV = "AIFORGE_DELEGATION_MAX_DEPTH"
_DEFAULT_MAX_DEPTH = 3


def _build_delegate_agent(role: str):
    """Construct a single LlmAgent for ``role`` using the production
    pipeline factory. Late import so this module is unit-testable
    without ADK present."""
    from aiforge_core.runtime.pipeline import build_pipeline

    pipeline = build_pipeline(skip_researcher=False)
    # The SequentialAgent exposes ``sub_agents``; find the one matching role.
    sub_agents = getattr(pipeline, "sub_agents", []) or []
    for sub in sub_agents:
        if getattr(sub, "name", "").lower() == role:
            return sub
    return None


def _cleanup_delegate_sessions(session_id) -> None:
    """Tear down the delegate's per-run tool sessions. A delegate inherits the
    full Doer toolset (bash/kernel/browser) and would otherwise leak those
    resources — this hand-rolled runner doesn't register the production runner's
    finish callbacks."""
    for mod, fn in (("bash", "destroy_session"),
                    ("ipython_kernel", "destroy_kernel"),
                    ("browser", "destroy_context")):
        try:
            m = __import__(f"aiforge_core.runtime.tools.{mod}", fromlist=[fn])
            getattr(m, fn)(session_id)
        except Exception:  # noqa: BLE001
            pass


async def _drive_delegate(runner, session_id, content) -> list[str]:
    """Run the delegate to completion, collecting its final-response text."""
    output_parts: list[str] = []
    async for event in runner.run_async(user_id="delegator",
                                        session_id=session_id,
                                        new_message=content):
        if event.is_final_response():
            txt = getattr(event, "text", "") or ""
            if txt:
                output_parts.append(txt)
    return output_parts


async def _run_delegate_async(role: str, prompt: str) -> dict[str, Any]:
    """Run one delegate to completion. NO deadline of its own — see
    :func:`_run_delegate_with_deadline`, which owns the wall clock."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    agent = _build_delegate_agent(role)
    if agent is None:
        return {"ok": False, "error": "delegate_build_failed", "role": role}

    session_svc = InMemorySessionService()
    runner = Runner(agent=agent, app_name="aiforge-delegate",
                    session_service=session_svc, auto_create_session=True)
    session = await session_svc.create_session(
        app_name="aiforge-delegate", user_id="delegator")
    content = gtypes.Content(role="user",
                             parts=[gtypes.Part.from_text(text=prompt)])
    # Key the delegate's tool sessions to its run so we can tear them down.
    try:
        from aiforge_core.runtime.tools.bash import set_run_id as _bash_set_run_id
        _bash_set_run_id(session.id)
    except Exception:  # noqa: BLE001
        pass

    try:
        output_parts = await _drive_delegate(runner, session.id, content)
    finally:
        # Runs on the deadline's cancellation too: asyncio.timeout() cancels
        # this task in place rather than abandoning it in a second one, so the
        # delegate's tool sessions are always torn down.
        _cleanup_delegate_sessions(session.id)

    session = await session_svc.get_session(
        app_name="aiforge-delegate", user_id="delegator", session_id=session.id)
    state = dict(session.state or {})
    return {"ok": True, "role": role, "output": "\n".join(output_parts),
            "state_keys": sorted(state.keys())}


async def _run_delegate_with_deadline(role: str, prompt: str,
                                      seconds: int) -> dict[str, Any]:
    """The wall clock lives HERE, not inside the delegate.

    A coroutine that takes its own ``timeout`` argument makes every caller
    inherit one policy; a context manager at the boundary lets the caller
    choose, and cancels the body in place so its ``finally`` still runs.
    """
    try:
        async with asyncio.timeout(seconds):
            return await _run_delegate_async(role, prompt)
    except TimeoutError:
        return {"ok": False, "error": "timeout", "role": role}


def delegate_to_agent(
    role: str,
    prompt: str,
    *,
    timeout: int = 600,
) -> dict[str, Any]:
    """Dispatch ``prompt`` to a single-agent ADK runner for ``role``.

    Caller blocks until the delegate returns or ``timeout`` (s) elapses.
    Soft-error contract.
    """
    role = (role or "").lower()
    if role not in _DELEGABLE_ROLES:
        return {"ok": False, "error": "unknown_role", "role": role,
                "allowed": sorted(_DELEGABLE_ROLES)}
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "empty_prompt"}

    # Depth is request-scoped (contextvar), NOT process-global env: under
    # concurrency two delegating chains must not see each other's depth (which
    # caused false ``delegation_depth_exceeded`` or runaway spawns). The cap
    # (max_depth) is config, so it stays in env.
    from aiforge_core.runtime import request_context
    current_depth = request_context.get_delegation_depth()
    try:
        max_depth = int(os.environ.get(_MAX_DEPTH_ENV, _DEFAULT_MAX_DEPTH))
    except ValueError:
        max_depth = _DEFAULT_MAX_DEPTH
    if current_depth >= max_depth:
        emit("Delegate", {"role": role, "skipped": True,
                          "reason": "depth_exceeded",
                          "depth": current_depth, "max_depth": max_depth})
        return {"ok": False, "error": "delegation_depth_exceeded",
                "role": role, "depth": current_depth,
                "max_depth": max_depth}

    started = time.monotonic()
    depth_token = request_context.enter_delegation()
    try:
        result = asyncio.run(
            _run_delegate_with_deadline(role, prompt, timeout))
    except Exception as exc:  # noqa: BLE001 — soft error
        return {"ok": False, "error": "delegate_failed",
                "role": role, "detail": str(exc)[:300]}
    finally:
        # Restore depth so a sibling delegate call sees the pre-increment value.
        request_context.reset_delegation(depth_token)
    wall_s = time.monotonic() - started
    emit("Delegate", {"role": role, "wall_s": round(wall_s, 2),
                      "depth": current_depth,
                      "ok": result.get("ok", False)})
    result["wall_s"] = round(wall_s, 2)
    result["depth"] = current_depth
    return result
