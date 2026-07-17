"""One-shot chat-completions client with provider fallback + cloud escalation.

KISS surface — just call :func:`complete`. Builds an OpenAI-compat
request body, posts to the resolved endpoint, returns
``message.content`` (or ``reasoning_content`` when content empty).

Three retry layers, in order:

1. **Pre-flight escalation** — if the prompt is too big for the local
   model's context window, :func:`router.escalate` returns a cloud
   Endpoint and the call is sent there directly. No local round-trip
   wasted.
2. **Quality fallback** — local 200-OK with empty content (mlx-lm tool
   call bug, garbage JSON) retries on the next available provider.
3. **Transport fallback** — connection / DNS / HTTP error retries on
   the next available provider once.

Per-call kwargs map to OpenAI body fields:
``temperature``, ``max_tokens``, ``top_p``, ``timeout_s``,
``extras`` (merged into body verbatim — pass
``{"chat_template_kwargs": {...}}`` for mlx-lm template kwargs).

This module was split (grouped by concern) into ``_helpers`` / ``_text`` /
``_errors`` / ``_http`` submodules; the call-orchestration pipeline
(``complete`` / ``_complete_impl`` / ``_try_post`` / ``_trace_generation`` /
``_is_fast_role``) stays defined here so the existing tests that
``monkeypatch.setattr("aiforge_core.llm.client.<name>", …)`` and rely on an
in-package consumer picking up the patch keep working unchanged. This package
re-exports the full former top-level surface (public AND private) so every
``client.<name>`` access — and ``from aiforge_core.llm.client import <name>`` —
is identical to before.
"""
from __future__ import annotations

import contextvars
import io
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

from .. import providers as _providers
from .. import rate_limiter as _rl
from .._ssl import _ca_bundle as _ssl_ca_bundle
from .._ssl import auto_relax_internal as _ssl_auto_relax
from .._ssl import context_for as _ssl_context_for
from .._ssl import insecure_context as _ssl_insecure
from ..router import escalate, fallback, resolve
from ..types import Endpoint
from ._errors import (
    _http_err_body,
    _is_transient_exc,
    _LLMCancelled,
    _MODEL_DROP_MARKERS,
    _ModelReloading,
    _raise_if_model_dropped,
    _TRANSIENT_HTTP,
)
from ._helpers import _estimate_tokens, _float_env, _int_env, _log, _record_usage
from ._http import (
    _build_body,
    _CANCEL,
    _NON_BODY_EXTRA_KEYS,
    _post,
    _post_cancellable,
    _post_ctx,
    _post_headers,
    _post_with_retry,
    _preflight,
    set_cancel_event,
)
from ._text import (
    _append_no_think,
    _extract_text,
    _is_garbage,
    _strip_think,
    _THINK_CLOSE_ONLY_RE,
    _THINK_LEAD_RE,
    _THINK_OPEN_RE,
)

__all__ = [
    "complete",
    "complete_raw",
    "set_cancel_event",
    "resolve",
    "escalate",
    "fallback",
    "Endpoint",
]


def _is_fast_role(role: str) -> bool:
    """Fast/direct-output role → pre-empt reasoning with /no_think from the
    start. Guarded import (registry optional); default False on any failure."""
    if os.environ.get("AIFORGE_FAST_ROLE_NO_THINK", "1") in ("0", "false", "no"):
        return False
    try:
        from aiforge_core.config.model_registry import is_fast_role
        return is_fast_role(role)
    except Exception:  # noqa: BLE001
        return False


def _try_post(ep: Endpoint, messages: list[dict],
              *, temperature, max_tokens, top_p, extras,
              timeout_s: int, role: str,
              source: str) -> tuple[str, dict] | None:
    """Attempt against ``ep``. Returns (text, raw_body) on success (text
    passing :func:`_is_garbage`), or ``None`` on transport error or persistent
    garbage. Caller decides whether to escalate / fall back.

    A 200-OK with empty / think-only content is intermittent on self-hosted
    reasoning models (qwen3-coder in particular): the same prompt re-issued to
    the SAME endpoint usually returns real content. With a single-model setup
    there is no fallback provider to fall over to, so retrying the same
    endpoint here is the only thing that turns a dropped learner capture or a
    stalled generation back into a real answer. Retry count is
    AIFORGE_LLM_EMPTY_RETRIES (default 2 → up to 3 total posts).
    """
    empty_retries = max(0, _int_env("AIFORGE_LLM_EMPTY_RETRIES", 3))
    # Fast/direct-output roles (learner/enhancer/triage/…) want a plain answer,
    # not a reasoning trace — if the configured model is a reasoning one it
    # returns EMPTY. Pre-empt that: coax /no_think from the FIRST attempt so
    # these roles don't burn a round discovering the empty. Thinking roles
    # (planner/architect/…) are untouched — they NEED the reasoning phase.
    fast_role = _is_fast_role(role)
    for attempt in range(empty_retries + 1):
        if attempt == 0:
            base = _append_no_think(messages) if fast_role else messages
            payload = _build_body(ep, base, temperature, max_tokens,
                                  top_p, extras)
        else:
            # Last post came back EMPTY. A reasoning model (qwen*-reasoning,
            # deepseek-r1) systematically spends its whole budget THINKING and
            # emits empty content — re-posting the identical body just repeats
            # that. Coax a DIRECT answer: append '/no_think' (Qwen/DeepSeek honor
            # it → skip the reasoning phase) and PROGRESSIVELY widen max_tokens
            # each retry (×2, ×3, …) so a still-thinking model always has room
            # left to emit the answer instead of us giving up on empty.
            _mt = min(int((max_tokens or 4096) * (attempt + 1)), 32768)
            payload = _build_body(ep, _append_no_think(messages), temperature,
                                  _mt, top_p, extras)
        try:
            body = _post_with_retry(ep, payload, timeout_s,
                                    role=role, source=source)
        except _LLMCancelled:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, TimeoutError, ValueError):
            # ValueError covers a non-JSON 200 (proxy HTML error page,
            # truncated / streaming body) so a malformed response falls back to
            # the next provider instead of crashing complete(). Transport
            # errors are NOT retried here — _post_with_retry already exhausted
            # its own transport retries; escalate to the next provider instead.
            return None
        _record_usage(role, body)
        text = _extract_text(body)
        # "[]"/"{}" is a valid answer only for fast/structured roles (learner
        # etc.), never for conversational chat/doer output.
        if not _is_garbage(text, allow_empty_json=fast_role):
            return text, body
        _log.warning(
            "llm.empty_response",
            extra={"aiforge": {"role": role, "provider": ep.provider,
                               "model": ep.model, "source": source,
                               "attempt": attempt + 1,
                               "retries": empty_retries,
                               "preview": text[:80]}},
        )
        if attempt < empty_retries:
            # Brief jittered pause so a momentarily-wedged model (mid-reload,
            # KV-cache thrash) gets a beat before the identical re-post.
            time.sleep(0.4 + random.random() * 0.6)
    return None


def _trace_generation(role: str, messages: list[dict], output: str,
                      latency_ms: int, error: str = "") -> None:
    """Mirror one completion to Langfuse when configured (env keys). Pure
    side-channel: soft-fails, never touches the call result. The file-based
    tracing (perf/observability/chat_trace) is unaffected and stays the
    source of truth."""
    try:
        from aiforge_core.integrations import langfuse_adapter as _lf
        if not _lf.enabled():
            return
        model = ""
        try:
            model = resolve(role).model
        except Exception:  # noqa: BLE001
            model = ""
        try:
            from aiforge_core.runtime.request_context import get_session_id
            _sid = get_session_id()
        except Exception:  # noqa: BLE001
            _sid = None
        _lf.record_generation(role=role, model=model, messages=messages,
                              output=output, latency_ms=latency_ms,
                              error=error, session_id=_sid)
    except Exception:  # noqa: BLE001 — tracing must never break a turn
        pass


def complete(role: str, messages: list[dict], *,
             temperature: float | None = None,
             max_tokens: int | None = None,
             top_p: float | None = None,
             extras: dict | None = None,
             timeout_s: int | None = None) -> str:
    """Timed wrapper around the LLM call — records wall-ms under family "LLM"
    keyed by ``role`` (stable), then delegates to the real implementation.
    Perf recording soft-fails and never affects the call result. When
    Langfuse env keys are set, every completion is also mirrored there
    (aiforge_core/integrations/langfuse_adapter)."""
    # Only the IMPORT is guarded — an exception raised from inside
    # _complete_impl must propagate, never trigger a SECOND (double-cost) call.
    try:
        from aiforge_core.runtime import perf_recorder
    except Exception:  # noqa: BLE001 — perf recording is optional
        perf_recorder = None
    import time as _time
    _t0 = _time.monotonic()
    try:
        if perf_recorder is not None:
            with perf_recorder.timed("LLM", role):
                out = _complete_impl(
                    role, messages, temperature=temperature,
                    max_tokens=max_tokens, top_p=top_p, extras=extras,
                    timeout_s=timeout_s,
                )
        else:
            out = _complete_impl(
                role, messages, temperature=temperature,
                max_tokens=max_tokens, top_p=top_p, extras=extras,
                timeout_s=timeout_s,
            )
    except Exception as exc:
        _trace_generation(role, messages, "",
                          int((_time.monotonic() - _t0) * 1000),
                          error=str(exc))
        raise
    _trace_generation(role, messages, out or "",
                      int((_time.monotonic() - _t0) * 1000))
    return out


def complete_raw(role: str, messages: list[dict], *,
                 tools: list | None = None,
                 tool_choice=None,
                 temperature: float | None = None,
                 max_tokens: int | None = None,
                 top_p: float | None = None,
                 extras: dict | None = None,
                 timeout_s: int | None = None) -> dict:
    """Native tool-calling completion. Returns the RAW assistant message dict
    (``{"role","content","tool_calls"?}``) instead of extracted text, so the
    caller can dispatch native ``tool_calls`` — the reliable alternative to the
    text ACTION/ARGS_JSON protocol (which local models fumble into
    ``ARGS_JSON: {}``). Bypasses the empty-content garbage filter because a
    tool-call reply legitimately has empty ``content``. Primary endpoint only —
    no cloud escalation (native FC is a local-model concern). Raises on
    transport failure or a malformed response so the caller falls back to text."""
    if timeout_s is None:
        timeout_s = _int_env("AIFORGE_LLM_TIMEOUT_S", 900)
    ex = dict(extras or {})
    if tools is not None:
        ex["tools"] = tools
    if tool_choice is not None:
        ex["tool_choice"] = tool_choice
    ep: Endpoint = resolve(role)
    payload = _build_body(ep, messages, temperature, max_tokens, top_p, ex)
    body = _post_with_retry(ep, payload, timeout_s, role=role, source="native")
    _record_usage(role, body)
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"native completion: no message in response ({exc})") from exc
    return dict(msg) if isinstance(msg, dict) else {"role": "assistant", "content": str(msg)}


def _complete_impl(role: str, messages: list[dict], *,
                   temperature: float | None = None,
                   max_tokens: int | None = None,
                   top_p: float | None = None,
                   extras: dict | None = None,
                   timeout_s: int | None = None) -> str:
    """Issue one chat-completion call for ``role`` with full retry chain.

    Order:
      1. Pre-flight cloud escalation if estimated tokens exceed local ctx
         window (router.escalate; no-op when already on cloud).
      2. POST primary (or escalated) endpoint. Success returns text.
      3. On transport error OR garbage 200-OK: try fallback() once.
      4. On fallback transport error too: try escalate(reason='quality').
      5. Exhausted: raise the original transport error if there was one,
         else RuntimeError("llm.exhausted").
    """
    # Default request timeout. Self-hosted reasoning models (e.g. 122B with
    # long chain-of-thought) routinely need minutes, so a short timeout shows up
    # as intermittent "timeout" transport errors ("model didn't respond").
    # Generous default (15 min), tunable via AIFORGE_LLM_TIMEOUT_S.
    if timeout_s is None:
        timeout_s = _int_env("AIFORGE_LLM_TIMEOUT_S", 900)

    primary: Endpoint = resolve(role)

    # Pre-flight escalation — if we can estimate token weight before
    # spending an LLM round-trip, do it. The estimator uses the same
    # 4-chars-per-token rule as the rate limiter.
    rough_payload = _build_body(
        primary, messages, temperature, max_tokens, top_p, extras,
    )
    est_tokens = _estimate_tokens(rough_payload)
    escalated = escalate(role, reason="context_overflow",
                         est_tokens=est_tokens)
    if escalated is not None:
        _log.info(
            "llm.escalated",
            extra={"aiforge": {"role": role,
                               "from": primary.provider,
                               "to": escalated.provider,
                               "est_tokens": est_tokens,
                               "reason": "context_overflow"}},
        )
        primary = escalated

    # Attempt 1 — primary
    out = _try_post(primary, messages,
                    temperature=temperature, max_tokens=max_tokens,
                    top_p=top_p, extras=extras,
                    timeout_s=timeout_s, role=role, source="primary")
    if out is not None:
        return out[0]

    # Attempt 2 — fallback() (different provider, same role)
    fb = fallback(role)
    if fb is not None and fb.provider != primary.provider:
        out = _try_post(fb, messages,
                        temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role, source="fallback")
        if out is not None:
            return out[0]

    # Attempt 3 — escalate on quality (forces cloud regardless of ctx)
    cloud = escalate(role, reason="quality")
    if cloud is not None and cloud.provider != primary.provider:
        out = _try_post(cloud, messages,
                        temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role,
                        source="quality_escalation")
        if out is not None:
            return out[0]

    raise RuntimeError(
        f"llm.exhausted role={role} primary={primary.provider}"
        f"@{primary.base_url} model={primary.model} "
        f"fallback={fb.provider if fb else 'none'} "
        f"cloud={cloud.provider if cloud else 'none'} "
        f"— all providers returned transport error or empty content "
        f"(see the llm.transport_error line above for the underlying cause)"
    )
