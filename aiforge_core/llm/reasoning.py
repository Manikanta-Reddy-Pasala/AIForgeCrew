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


__all__ = ["reasoning_off", "no_think_request", "NO_THINK_KWARGS"]
