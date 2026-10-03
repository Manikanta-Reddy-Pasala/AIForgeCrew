"""Retry / transient-error / empty-response policy helpers.

Split out of the former single-module ``escalating_llm``; behaviour identical.
"""
from __future__ import annotations

from typing import Any

from google.adk.models.llm_response import LlmResponse

from aiforge_core.llm import retry_policy


# Substrings that mark a TRANSIENT failure worth retrying the SAME endpoint
# with backoff. Includes 401/403 on purpose — the self-hosted proxy
# (nginx) returns intermittent "401 Authorization Required" even with a
# valid token; bounded retries ride over the blip instead of surfacing an
# "agent error" in the UI. Truly-bad creds still stop after the cap.
_TRANSIENT_MARKERS = (
    "authenticationerror", "401", "403", "authorization required",
    "timeout", "timedout", "timed out", "connection", "econnreset", "reset",
    "temporarily", "unavailable", "rate limit", "ratelimit", "429",
    "500", "502", "503", "504", "bad gateway", "gateway", "overloaded",
    "internalservererror", "apiconnectionerror", "serviceunavailable",
    "jsondecodeerror", "unterminated", "remotedisconnected", "broken pipe",
    # Model-lifecycle drops on ANY local OpenAI-compatible server (mlx-lm,
    # ollama, llama.cpp, vLLM, LM Studio, …): idle-unload, OOM-evict, restart,
    # or a not-yet-loaded model. Retrying (→ primary_retry / next candidate)
    # lets the server reload it instead of hard-failing the run.
    "model unloaded", "unloaded", "model not loaded", "not loaded",
    "model not found", "model_not_found", "no model", "no models loaded",
    "model is loading", "loading model", "model not ready", "still loading",
)


# The retry knobs live in llm/retry_policy.
_attempt_retries = retry_policy.attempt_retries
_demote_after = retry_policy.demote_after


def _is_transient_llm_error(exc: Exception) -> bool:
    s = (type(exc).__name__ + " " + str(exc)).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


# What a server says when the requested model id is not one it serves. LM
# Studio answers an unknown id with the same "no models loaded" wording it uses
# for an idle-unloaded one, and a strict server answers 404 / "model not found".
_MODEL_MISSING_MARKERS = (
    "no models loaded",
    "model not found",
    "does not exist",
    "unknown model",
    "invalid model",
)


def _looks_like_missing_model(exc: BaseException) -> bool:
    """Does this failure say the MODEL is wrong (rather than the box being
    down)? String-matched on purpose: the pipeline reaches the endpoint through
    LiteLlm, which flattens the provider's error into a message."""
    s = (type(exc).__name__ + " " + str(exc)).lower()
    return any(m in s for m in _MODEL_MISSING_MARKERS)


def _api_base_of(model: Any) -> str:
    """Best-effort endpoint URL for a built model. ADK's LiteLlm doesn't expose
    ``api_base`` directly — it lives in ``_additional_args`` — so ``getattr``
    alone logged ``?`` and the LM-crash recovery couldn't find the endpoint."""
    base = getattr(model, "api_base", None)
    if not base:
        extra = getattr(model, "_additional_args", None)
        if isinstance(extra, dict):
            base = extra.get("api_base")
    return base or ""


def _is_empty(resp: LlmResponse) -> bool:
    """A 200-OK that's actually useless — no text, no tool calls.

    Think-only replies count as empty: a reasoning model (qwen3-coder) that
    emitted a ``<think>…</think>`` block and then ran out of budget before
    writing an answer leaves part.text non-empty but content-free. Stripping
    the think block collapses it to "" here, so the caller retries the next
    candidate (or the trailing ``primary_retry`` slot in a single-model setup)
    for a real answer instead of passing raw chain-of-thought to the agent.
    """
    if resp.error_code:
        return True
    content = getattr(resp, "content", None)
    if content is None:
        return True
    from aiforge_core.llm.client import strip_think
    parts = getattr(content, "parts", None) or []
    has_signal = False
    for p in parts:
        text = getattr(p, "text", None)
        if text and strip_think(text.strip()):
            has_signal = True
            break
        if getattr(p, "function_call", None):
            has_signal = True
            break
    return not has_signal
