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
from dataclasses import replace
from typing import cast

from .. import providers as _providers
from .. import rate_limiter as _rl
from .._ssl import _ca_bundle as _ssl_ca_bundle
from .._ssl import auto_relax_internal as _ssl_auto_relax
from .._ssl import context_for as _ssl_context_for
from .._ssl import insecure_context as _ssl_insecure
from ..router import escalate, fallback, resolve
from ..types import Endpoint
from ._attempt import (  # noqa: F401  # re-exported
    _attempt_payload,
    _is_fast_role,
    _jitter,
    _note_transport_failure,
    _record_empty,
    _try_post,
)
from ._chain import (  # noqa: F401  # re-exported
    _chain_endpoint,
    _chain_rows,
    _has_non_text_content,
    _model_chain_blocked,
    _model_chain_enabled,
    _native_chain_post,
    _native_model_chain,
    _try_model_chain,
)
from ._errors import (
    _MODEL_DROP_MARKERS,
    _TRANSIENT_HTTP,
    _http_err_body,
    _is_transient_exc,
    _LLMCancelled,
    _ModelReloading,
    _raise_if_model_dropped,
)
from ._helpers import _estimate_tokens, _float_env, _int_env, _log, _record_usage
from ._http import (
    _CANCEL,
    _NON_BODY_EXTRA_KEYS,
    _build_body,
    _post,
    _post_cancellable,
    _post_ctx,
    _post_headers,
    _post_with_retry,
    _preflight,
    set_cancel_event,
    set_delta_sink,
    shipped_timeout,  # re-export: callers above the transport
)
from ._http import TIMEOUT_SHIPPED_ATTR as _TIMEOUT_SHIPPED_ATTR
from ._http import shipped_timeout as _shipped_timeout
from ._missing import (  # noqa: F401  # re-exported
    _autofallback_enabled,
    _diagnose_missing,
    _exhausted_error,
    _log_model_missing,
    _looks_like_a_model_error,
    _model_missing_error,
    _substitute_attempt,
)
from ._models import MODEL_MISSING_ATTR, model_missing
from ._text import (
    _THINK_CLOSE_ONLY_RE,
    _THINK_LEAD_RE,
    _THINK_OPEN_RE,
    _append_no_think,
    _extract_text,
    _is_garbage,
    _strip_think,
)

__all__ = [
    "complete",
    "complete_raw",
    "set_cancel_event",
    "set_delta_sink",
    "resolve",
    "escalate",
    "fallback",
    "model_missing",
    "Endpoint",
]


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
    # The meter token, threaded exactly as `_try_post` does it. Without it the
    # tokens land machine-wide but on NO turn — and this is the DEFAULT chat
    # path (AIFORGE_CHAT_TOOL_PROTOCOL=native), so "how much did this message
    # write" read 0 for almost every real message while the session total
    # climbed. A per-turn number that is always zero is worse than none.
    _meter_tok: list = [None]
    # Timed like complete(): this is the DEFAULT chat path, and the Perf page
    # recorded no LLM time at all for it.
    import time as _time
    _t0 = _time.perf_counter()
    try:
        body = _post_with_retry(ep, payload, timeout_s, role=role,
                                source="native", meter=_meter_tok)
    except _LLMCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        # THE DEFAULT CHAT PATH. AIFORGE_CHAT_TOOL_PROTOCOL defaults to
        # "native", so simple chat comes through HERE, not through complete().
        # A fallback chain that only existed on the other path was a fallback
        # the user could never actually reach: "chat picks one model and if it
        # fails it should go to the others" is exactly this function.
        body = _native_model_chain(role, ep, payload, timeout_s,
                                   meter=_meter_tok)
        if body is None:
            _perf_record("LLM", role, _t0)
            raise exc
    _perf_record("LLM", role, _t0)
    _record_usage(role, body, _meter_tok[0])
    try:
        choice = body["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"native completion: no message in response ({exc})") from exc
    out = dict(msg) if isinstance(msg, dict) else {"role": "assistant", "content": str(msg)}
    # Carried for the reader, never sent back: "length" with no content means
    # the model ran out of tokens while still reasoning (see _text._extract_text).
    out["_finish_reason"] = choice.get("finish_reason") if isinstance(choice, dict) else None
    return out


def _perf_record(family: str, name: str, t0: float) -> None:
    """One perf sample since ``t0`` (perf_counter). Never raises."""
    try:
        import time as _time

        from aiforge_core.runtime import perf_recorder
        perf_recorder.record(family, name, (_time.perf_counter() - t0) * 1000.0)
    except Exception:  # noqa: BLE001 — perf recording is optional
        pass


def _preflight_escalate(role: str, primary: Endpoint, messages: list[dict],
                        temperature, max_tokens, top_p, extras):
    """Escalate BEFORE spending a round-trip when the estimated token weight
    exceeds the local context window. The estimator uses the same
    4-chars-per-token rule as the rate limiter. Returns the (possibly new)
    endpoint and whether it changed."""
    est_tokens = _estimate_tokens(_build_body(
        primary, messages, temperature, max_tokens, top_p, extras))
    escalated = escalate(role, reason="context_overflow", est_tokens=est_tokens)
    if escalated is None:
        return primary, False
    _log.info("llm.escalated",
              extra={"aiforge": {"role": role, "from": primary.provider,
                                 "to": escalated.provider,
                                 "est_tokens": est_tokens,
                                 "reason": "context_overflow"}})
    return escalated, True


def _provider_chain(role: str, primary: Endpoint, messages: list[dict],
                    shipped: dict, post: dict):
    """The three provider attempts, in order: primary, then fallback() (a
    different provider for the same role), then escalate on quality (forces
    cloud regardless of ctx). Returns ``(text_or_None, fb, cloud)`` — the two
    endpoints are handed back for the exhausted-error message."""
    out = _try_post(primary, messages, shipped=shipped, source="primary", **post)
    if out is not None:
        return out[0], None, None
    fb = fallback(role)
    if fb is not None and fb.provider != primary.provider:
        out = _try_post(fb, messages, shipped=shipped, source="fallback", **post)
        if out is not None:
            return out[0], fb, None
    cloud = escalate(role, reason="quality")
    if cloud is not None and cloud.provider != primary.provider:
        out = _try_post(cloud, messages, shipped=shipped,
                        source="quality_escalation", **post)
        if out is not None:
            return out[0], fb, cloud
    return None, fb, cloud


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
      5. The other models the operator configured, one attempt each.
      6. Exhausted: raise the original transport error if there was one,
         else RuntimeError("llm.exhausted").
    """
    # Default request timeout. Self-hosted reasoning models (e.g. 122B with
    # long chain-of-thought) routinely need minutes, so a short timeout shows up
    # as intermittent "timeout" transport errors ("model didn't respond").
    # Generous default (15 min), tunable via AIFORGE_LLM_TIMEOUT_S.
    if timeout_s is None:
        timeout_s = _int_env("AIFORGE_LLM_TIMEOUT_S", 900)
    primary: Endpoint = resolve(role)
    # The role's OWN endpoint, kept before any escalation rebinds `primary`.
    chain_base = primary
    primary, escalated_for_overflow = _preflight_escalate(
        role, primary, messages, temperature, max_tokens, top_p, extras)

    post = {"temperature": temperature, "max_tokens": max_tokens,
            "top_p": top_p, "extras": extras, "timeout_s": timeout_s,
            "role": role}
    shipped: dict = {}
    text, fb, cloud = _provider_chain(role, primary, messages, shipped, post)
    if text is not None:
        return text

    # DIAGNOSE FIRST, rescue second. Run before the registry chain so a role
    # pointed at a model id the endpoint does not serve is REPORTED even when
    # another configured model then answers: otherwise every turn is quietly
    # served by a model the operator did not select, at WARNING level only,
    # forever.
    missing_now = _diagnose_missing(shipped, chain_base)
    if missing_now:
        _log_model_missing(role, chain_base, missing_now)

    # THE OTHER MODELS THE OPERATOR CONFIGURED. "I added four models; when the
    # one chat picked stops answering it should try the others" — until now the
    # registry was a selection list only, and the provider fallback chain is for
    # CLOUD escalation (empty without a cloud key), so on a single-provider
    # install a dead model was simply the end.
    #
    # Deliberately AFTER the same-provider fallback and the quality escalation,
    # and one attempt per model: this is a rescue, not a routing policy. Chained
    # off the endpoint the ROLE is configured with, not off `primary`: a
    # context_overflow escalation rebinds `primary` to the cloud, and chaining
    # from there would offer the failed local model back to itself, send local
    # model ids (with the cloud key) to the vendor, and re-send a prompt that
    # was escalated precisely because it does not fit locally.
    chain_tried = 0
    if not escalated_for_overflow:
        tried: list = []
        out = _try_model_chain(role, chain_base, messages,
                               temperature=temperature, max_tokens=max_tokens,
                               top_p=top_p, extras=extras, timeout_s=timeout_s,
                               shipped=shipped, tried=tried)
        chain_tried = len(tried)
        if out is not None:
            return out

    # Before blaming the network: is the configured model even served here? A
    # role pointed at a model id the box does not have fails with model-
    # lifecycle wording ("No models loaded"), which reads as transient, so every
    # layer retries a permanent config error — and the user is told "the model
    # didn't respond", naming neither the model nor the endpoint. One cheap GET
    # turns that into the sentence that fixes it.
    missing = _diagnose_missing(shipped, primary)
    if missing:
        out = _substitute_attempt(role, primary, missing, messages, temperature,
                                  max_tokens, top_p, extras, timeout_s)
        if out is not None:
            return out
    if missing is not None:
        raise _model_missing_error(role, primary, missing)
    raise _exhausted_error(role, primary, fb, cloud, chain_tried, shipped)
