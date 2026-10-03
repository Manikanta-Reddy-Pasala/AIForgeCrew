"""Turning a model's reasoning phase OFF — every request, every path.

A reasoning model that thinks before answering costs minutes on a local box and,
when it runs out of tokens mid-thought, leaves no answer at all. The operator
turns it off per model (Models → Thinking: no) or everywhere
(``AIFORGE_NO_REASONING=1``); then every request to that model carries both
switches the Qwen/DeepSeek family honours: the chat-template kwarg
``enable_thinking: false`` and the ``/no_think`` soft switch on the last user
turn. (Measured on LM Studio 0.4 + qwen3.8-27b: 0 reasoning tokens.)

The direct client applies it in ``_build_body``; the ADK pipeline in the
LiteLlm builder (the kwarg) and ``EscalatingLlm._stamp_request`` (/no_think).
"""
from __future__ import annotations

import contextlib
import contextvars
import os

NO_THINK_KWARGS = {"chat_template_kwargs": {"enable_thinking": False}}

#: The OpenAI-style request field that asks a server to limit reasoning.
EFFORT_FIELD = "reasoning_effort"
_LEVELS = ("none", "low", "medium", "high")


#: Roles that may reason. Every other role is told not to: a reasoning phase on
#: each agent step costs minutes on a local box, and only planning (deciding
#: what to do) benefits from it. Operators change the set with
#: ``AIFORGE_REASONING_ROLES`` (comma-separated role names).
DEFAULT_REASONING_ROLES = ("planner",)


def reasoning_roles() -> "tuple[str, ...]":
    raw = os.environ.get("AIFORGE_REASONING_ROLES")
    if raw is None:
        return DEFAULT_REASONING_ROLES
    return tuple(r.strip().lower() for r in raw.split(",") if r.strip())


_BOOST: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "aiforge_reasoning_boost", default=False)


@contextlib.contextmanager
def boost(active: bool = True):
    """Let the calls made inside this block reason, whatever their role.

    Reasoning is off for most roles because most steps do not need it and it
    costs minutes. A step that is STUCK does: the loop guards and the retries
    after a stall turn it on for the next few calls (the task's difficulty
    decides, not the model's name)."""
    token = _BOOST.set(bool(active))
    try:
        yield
    finally:
        _BOOST.reset(token)


def boosted() -> bool:
    return _BOOST.get()


def boost_steps() -> int:
    """How many model calls a stall turns reasoning on for
    (``AIFORGE_STUCK_REASON_STEPS``, default 6; 0 never boosts)."""
    from aiforge_core.runtime.stuck_policy import Policy
    return Policy.load().reason_steps


def role_reasons(role: str = "") -> bool:
    """True when ``role`` may reason: a reasoning role, an unnamed role, or any
    role inside a :func:`boost` block."""
    role = (role or "").strip().lower()
    return not role or role in reasoning_roles() or _BOOST.get()


def reasoning_off(model: str, base_url: str = "", role: str = "") -> bool:
    """True when requests to ``model`` (made for ``role``) must not reason."""
    env = os.environ.get("AIFORGE_NO_REASONING", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if not role_reasons(role):
        return True
    try:
        from aiforge_core.config import model_registry
        return model_registry.thinking_for(model, base_url) == "no"
    except Exception:  # noqa: BLE001 — a registry problem never breaks a call
        return False


def effort_for(model: str, base_url: str = "", role: str = "") -> "str | None":
    """The ``reasoning_effort`` to send, or None for the server's own default.

    ``none`` when reasoning is off for this request (see :func:`reasoning_off`);
    else ``AIFORGE_REASONING_EFFORT`` (none|low|medium|high) if set.

    The chat-template kwarg and ``/no_think`` are ignored by some servers (LM
    Studio's Qwen3.x); the top-level OpenAI-style field is the switch they
    honour, so it is sent as well."""
    if reasoning_off(model, base_url, role):
        return "none"
    env = os.environ.get("AIFORGE_REASONING_EFFORT", "").strip().lower()
    return env if env in _LEVELS else None


def effort_extras(model: str, base_url: str = "", role: str = "") -> dict:
    """``{"reasoning_effort": …}`` for this request, unless the server already
    refused that field for it (then nothing: see fast_reasoning.note_rejection)."""
    eff = effort_for(model, base_url, role)
    if not eff:
        return {}
    try:
        from aiforge_core.llm import fast_reasoning
        if fast_reasoning.rejected(base_url, model):
            return {}
    except Exception:  # noqa: BLE001
        pass
    return {EFFORT_FIELD: eff}


def no_think_request(llm_request):
    """An ADK LlmRequest whose last user text ends with ``/no_think`` (a copy;
    the original is untouched). Unchanged when there is no user text."""
    try:
        req = llm_request.model_copy(deep=True)
        for content in reversed(req.contents or []):
            if getattr(content, "role", "") != "user":
                continue
            for part in reversed(content.parts or []):
                text = getattr(part, "text", None)
                if text:
                    if not text.rstrip().endswith("/no_think"):
                        part.text = text.rstrip() + " /no_think"
                    return req
        return llm_request
    except Exception:  # noqa: BLE001
        return llm_request


__all__ = ["reasoning_off", "no_think_request", "NO_THINK_KWARGS", "EFFORT_FIELD",
           "effort_for", "effort_extras", "role_reasons", "reasoning_roles",
           "boost", "boosted", "boost_steps", "boost_reserve_tokens"]


def boost_reserve_tokens(_role: str = "") -> int:
    """Reply tokens to keep free while a boosted step runs: a reasoning call
    writes far more than a plain one, and must not overflow the window
    (``AIFORGE_BOOST_RESERVE_TOKENS``, default 8192; 0 = none)."""
    if not boosted():
        return 0
    try:
        return max(0, int(os.environ.get("AIFORGE_BOOST_RESERVE_TOKENS", "8192")))
    except ValueError:
        return 8192

