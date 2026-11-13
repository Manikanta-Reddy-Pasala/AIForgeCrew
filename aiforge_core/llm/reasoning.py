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

#: Chat-template switches: Qwen3 / GLM templates read ``enable_thinking``,
#: DeepSeek V3.1+ and Kimi K2.5 templates (vLLM, SGLang, LM Studio) read
#: ``thinking``. A template ignores a variable it does not use.
NO_THINK_KWARGS = {"chat_template_kwargs": {"enable_thinking": False, "thinking": False}}

#: The hosted-API switch (Moonshot Kimi, DeepSeek, Z.ai GLM and other
#: OpenAI-compatible APIs that follow the Anthropic-style field).
THINKING_FIELD = "thinking"
THINKING_OFF = {"type": "disabled"}

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


_OFF: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "aiforge_reasoning_forced_off", default=False)


@contextlib.contextmanager
def forced_off():
    """The calls made inside this block do not reason, whatever the role, the
    model setting or a stuck boost says: the answer-only retry after a reply
    spent its whole length thinking."""
    token = _OFF.set(True)
    try:
        yield
    finally:
        _OFF.reset(token)


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
    if _OFF.get():
        return True
    env = os.environ.get("AIFORGE_NO_REASONING", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if not role_reasons(role):
        return True
    return model_setting(model, base_url) == "no"


def model_setting(model: str, base_url: str = "") -> "str | None":
    """Models → Thinking for ``model``: 'yes' | 'no' | 'low' | 'medium' |
    'high', or None (auto / not registered)."""
    try:
        from aiforge_core.config import model_registry
        return model_registry.thinking_for(model, base_url)
    except Exception:  # noqa: BLE001 — a registry problem never breaks a call
        return None


#: Reasoning tokens a Low / Medium model may spend before the harness asks
#: for the answer without it, for servers that ignore ``reasoning_effort``
#: (Kimi, DeepSeek). High and On are not capped here.
_CAP_ENV = {"low": ("AIFORGE_THINKING_LOW_TOKENS", 1024),
            "medium": ("AIFORGE_THINKING_MEDIUM_TOKENS", 4096)}


def cap_tokens(model: str, base_url: str = "", role: str = "") -> int:
    """The reasoning cap in tokens for this request; 0 = none."""
    if reasoning_off(model, base_url, role):
        return 0
    level = model_setting(model, base_url)
    if level not in _CAP_ENV:
        return 0
    key, default = _CAP_ENV[level]
    try:
        return max(0, int(os.environ.get(key, default)))
    except ValueError:
        return default


def effort_for(model: str, base_url: str = "", role: str = "") -> "str | None":
    """The ``reasoning_effort`` to send, or None for the server's own default.

    ``none`` when reasoning is off for this request (see :func:`reasoning_off`);
    else ``AIFORGE_REASONING_EFFORT`` (none|low|medium|high) if set.

    The chat-template kwarg and ``/no_think`` are ignored by some servers (LM
    Studio's Qwen3.x); the top-level OpenAI-style field is the switch they
    honour, so it is sent as well."""
    if reasoning_off(model, base_url, role):
        return "none"
    level = model_setting(model, base_url)
    if level in ("low", "medium", "high"):
        return level
    env = os.environ.get("AIFORGE_REASONING_EFFORT", "").strip().lower()
    return env if env in _LEVELS else None


def effort_extras(model: str, base_url: str = "", role: str = "") -> dict:
    """``{"reasoning_effort": …}`` for this request, unless the server already
    refused that field for it (then nothing: see fast_reasoning.note_rejection)."""
    eff = effort_for(model, base_url, role)
    if not eff:
        return {}
    return _unrejected(base_url, model, {EFFORT_FIELD: eff})


def _unrejected(base_url: str, model: str, body: dict) -> dict:
    """``body`` without the fields this server already refused for ``model``."""
    try:
        from aiforge_core.llm import fast_reasoning
        return {k: v for k, v in body.items()
                if not fast_reasoning.rejected(base_url, model, k)}
    except Exception:  # noqa: BLE001
        return body


def off_fields(model: str, base_url: str = "", hosted_switch: bool = True) -> dict:
    """Every OpenAI-compatible request-body switch that turns reasoning off:
    the chat-template kwargs (self-hosted), ``thinking: {"type": "disabled"}``
    (hosted Kimi / DeepSeek / GLM APIs) and ``reasoning_effort: none``
    (OpenAI, LM Studio, Ollama), minus any this server refused before."""
    body = {**NO_THINK_KWARGS, EFFORT_FIELD: "none"}
    if hosted_switch:
        body[THINKING_FIELD] = dict(THINKING_OFF)
    return _unrejected(base_url, model, body)


def off_extras(model: str, base_url: str = "") -> dict:
    """The request-body switches that turn reasoning OFF for one request,
    whatever its role (the ADK/LiteLLM path, which has no 400-and-resend: the
    hosted ``thinking`` field only when the operator set Thinking: Off for
    this model)."""
    return off_fields(model, base_url,
                      hosted_switch=model_setting(model, base_url) == "no")


def budget_tokens() -> int:
    """How long a reasoning phase may run before the answer is asked for
    without it (``AIFORGE_REASONING_BUDGET_TOKENS``; 0 = no limit).

    Reasoning and the answer share one reply cap. A phase that runs to the cap
    leaves no answer at all — and takes minutes to get there. Default: a
    quarter of the reply cap; what was reasoned by then is kept as notes for
    the answer, so the cut costs little."""
    raw = os.environ.get("AIFORGE_REASONING_BUDGET_TOKENS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    try:
        from aiforge_core.config import runtime_settings
        return max(0, int(runtime_settings.get("max_output_tokens")) // 4)
    except Exception:  # noqa: BLE001
        return 2048


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
           "THINKING_FIELD", "off_fields", "model_setting", "cap_tokens",
           "effort_for", "effort_extras", "role_reasons", "reasoning_roles",
           "boost", "boosted", "boost_steps", "boost_reserve_tokens",
           "off_extras", "budget_tokens"]


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

