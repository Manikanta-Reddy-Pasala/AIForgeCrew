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

import os

NO_THINK_KWARGS = {"chat_template_kwargs": {"enable_thinking": False}}

#: The OpenAI-style request field that asks a server to limit reasoning.
EFFORT_FIELD = "reasoning_effort"
_LEVELS = ("none", "low", "medium", "high")


def reasoning_off(model: str, base_url: str = "") -> bool:
    """True when requests to ``model`` must not reason."""
    env = os.environ.get("AIFORGE_NO_REASONING", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        from aiforge_core.config import model_registry
        return model_registry.thinking_for(model, base_url) == "no"
    except Exception:  # noqa: BLE001 — a registry problem never breaks a call
        return False


def effort_for(model: str, base_url: str = "") -> "str | None":
    """The ``reasoning_effort`` to send to ``model``, or None for the server's own
    default.

    ``none`` when reasoning is switched off (Models -> Thinking: no, or
    AIFORGE_NO_REASONING=1); ``low`` when the model is set to Thinking: low;
    else ``AIFORGE_REASONING_EFFORT`` (none|low|medium|high) if set.

    The chat-template kwarg and ``/no_think`` do not stop Qwen3.8 reasoning on
    LM Studio (measured: the same 27 reasoning chunks with and without them);
    only this top-level field does, so it is sent as well. On a coding agent a
    reasoning model spent 96% of what it generated thinking and took 9x longer
    for the same task."""
    if reasoning_off(model, base_url):
        return "none"
    try:
        from aiforge_core.config import model_registry
        if model_registry.thinking_for(model, base_url) == "low":
            return "low"
    except Exception:  # noqa: BLE001
        pass
    env = os.environ.get("AIFORGE_REASONING_EFFORT", "").strip().lower()
    return env if env in _LEVELS else None


def effort_extras(model: str, base_url: str = "") -> dict:
    """``{"reasoning_effort": …}`` for this model, unless the server already
    refused that field for it (then nothing: see fast_reasoning.note_rejection)."""
    eff = effort_for(model, base_url)
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
           "effort_for", "effort_extras"]
