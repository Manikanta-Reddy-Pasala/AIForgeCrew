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

Depth cap (sub #16): each delegate runs with ``AIFORGE_DELEGATION_DEPTH``
incremented; calls beyond ``AIFORGE_DELEGATION_MAX_DEPTH`` (default 3)
return ``{ok: False, error: "delegation_depth_exceeded"}`` instead of
spawning. Prevents runaway recursive delegation if a model decides to
always delegate.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from ._trace import emit

_DELEGABLE_ROLES = {"researcher", "planner", "refiner", "triage", "verifier"}
_DEPTH_ENV = "AIFORGE_DELEGATION_DEPTH"
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


async def _run_delegate_async(
    role: str, prompt: str, timeout: int,
) -> dict[str, Any]:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as gtypes

    agent = _build_delegate_agent(role)
    if agent is None:
        return {"ok": False, "error": "delegate_build_failed", "role": role}

    session_svc = InMemorySessionService()
    runner = Runner(
        agent=agent, app_name="aiforge-delegate",
        session_service=session_svc, auto_create_session=True,
    )
    session = await session_svc.create_session(
        app_name="aiforge-delegate", user_id="delegator",
    )
    content = gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text=prompt)],
    )

    async def _drive():
        output_parts: list[str] = []
        async for event in runner.run_async(
            user_id="delegator", session_id=session.id, new_message=content,
        ):
            if event.is_final_response():
                txt = getattr(event, "text", "") or ""
                if txt:
                    output_parts.append(txt)
        return output_parts

    try:
        output_parts = await asyncio.wait_for(_drive(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "role": role}

    session = await session_svc.get_session(
        app_name="aiforge-delegate", user_id="delegator",
        session_id=session.id,
    )
    state = dict(session.state or {})
    return {
        "ok": True,
        "role": role,
        "output": "\n".join(output_parts),
        "state_keys": sorted(state.keys()),
    }


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

    try:
        current_depth = int(os.environ.get(_DEPTH_ENV, "0"))
    except ValueError:
        current_depth = 0
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
    prev_depth_env = os.environ.get(_DEPTH_ENV)
    os.environ[_DEPTH_ENV] = str(current_depth + 1)
    try:
        result = asyncio.run(_run_delegate_async(role, prompt, timeout))
    except Exception as exc:  # noqa: BLE001 — soft error
        return {"ok": False, "error": "delegate_failed",
                "role": role, "detail": str(exc)[:300]}
    finally:
        # Restore depth env so the Doer's next call sees the same value.
        if prev_depth_env is None:
            os.environ.pop(_DEPTH_ENV, None)
        else:
            os.environ[_DEPTH_ENV] = prev_depth_env
    wall_s = time.monotonic() - started
    emit("Delegate", {"role": role, "wall_s": round(wall_s, 2),
                      "depth": current_depth,
                      "ok": result.get("ok", False)})
    result["wall_s"] = round(wall_s, 2)
    result["depth"] = current_depth
    return result
