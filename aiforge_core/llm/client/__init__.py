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

from dataclasses import replace

import contextvars
import io
import json
import logging
import os
import random

_jitter = random.SystemRandom()
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
from ._http import TIMEOUT_SHIPPED_ATTR as _TIMEOUT_SHIPPED_ATTR
from ._models import MODEL_MISSING_ATTR, model_missing
from ._http import shipped_timeout as _shipped_timeout
from ._http import shipped_timeout  # re-export: callers above the transport
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
    "model_missing",
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


def _record_empty(token) -> None:
    """One counted request answered with nothing. Never raises; a falsy token
    means the send itself was never counted, so the failure must not be."""
    if not token:
        return
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record_failure(token, "empty")
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass


def _model_chain_enabled() -> bool:
    """Try the operator's other configured models when the chosen one fails.

    On by default — four models were configured precisely so that one of them
    answering is enough. ``AIFORGE_LLM_MODEL_CHAIN=0`` restores "the selected
    model or nothing", which is the right setting when a run must be
    attributable to one exact model.
    """
    import os as _os
    return _os.environ.get("AIFORGE_LLM_MODEL_CHAIN", "1") not in (
        "0", "false", "no")


def _has_non_text_content(messages: list[dict]) -> bool:
    """Does this request carry image / multimodal parts?

    A vision call reached its role BECAUSE that model can see. Falling through
    to the operator's other models re-uploads multi-MB base64 to text-only
    endpoints, and the dangerous outcome is not the waste: a server that
    silently drops an unrecognised image block answers with a plausible
    caption of an image it never saw.
    """
    for m in messages or []:
        if isinstance(m.get("content"), list):
            return True
    return False


def _try_model_chain(role: str, primary: Endpoint, messages: list[dict], *,
                     temperature, max_tokens, top_p, extras,
                     timeout_s: int, shipped: dict | None = None,
                     tried: list | None = None) -> "str | None":
    """One attempt against each OTHER configured model, in registry order.

    A row that names its own base_url is a DIFFERENT CONNECTION and is taken
    whole — its own key, its own TLS setting. Inheriting the primary's would
    put one endpoint's credential on another host's wire (and, through
    ``extras``, strip TLS verification from a public endpoint because a LAN box
    was marked insecure). A row without a base_url is the same endpoint with a
    different model id, which is the common case for several models loaded on
    one local server.
    """
    if not _model_chain_enabled():
        return None
    if shipped and shipped.get("timeout"):
        # The prompt REACHED a model and was abandoned on a read timeout. Every
        # layer in this stack refuses to re-issue that; the chain must not be
        # the one place that does it N more times.
        return None
    if _has_non_text_content(messages):
        return None
    try:
        from aiforge_core.config import model_registry
        rows = model_registry.chain_after(primary.model, primary.base_url)
    except Exception:  # noqa: BLE001 — the registry is optional
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue                     # one malformed row is not a reason to
        mid = str(row.get("model") or "").strip()   # abandon every other model
        if not mid:
            continue
        # str() first: a hand-edited registry can hold a number here, and
        # `.strip()` on it raised an AttributeError from inside the LLM client
        # in place of the informative llm.exhausted the caller expects.
        _row_url = str(row.get("base_url") or "").strip()
        if _row_url and _row_url.rstrip("/") != (primary.base_url or "").rstrip("/"):
            # Another host: take the connection whole. An empty key stays
            # EMPTY — a keyed primary must not lend its credential — and the
            # row's own insecure_tls decides its TLS, not the primary's.
            _ex = dict(primary.extras or {})
            _ex.pop("insecure_tls", None)
            if row.get("insecure_tls"):
                _ex["insecure_tls"] = True
            ep = replace(primary, model=mid, base_url=_row_url,
                         api_key=str(row.get("api_key") or ""), extras=_ex)
        else:
            ep = replace(primary, model=mid)
        _log.warning(
            "llm.model_chain_try role=%s failed=%s trying=%s endpoint=%s — "
            "the selected model did not answer; falling through to the next "
            "configured model",
            role, primary.model, mid, ep.base_url,
            extra={"aiforge": {"role": role, "failed": primary.model,
                               "trying": mid, "endpoint": ep.base_url}},
        )
        if tried is not None:
            tried.append(mid)
        # ONE post per chain model. The empty-retry ladder is for the model
        # the operator CHOSE; multiplying it across the chain turned one
        # message into 16+ full generations (4 models x 4 posts), and the
        # chat loop then re-issues the whole call up to five more times.
        out = _try_post(ep, messages, shipped=(shipped if shipped is not None else {}),
                        empty_retries=0,
                        temperature=temperature,
                        max_tokens=max_tokens, top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role, source="model_chain")
        if out is not None:
            _log.warning("llm.model_chain_used role=%s model=%s (selected %s "
                         "did not answer)", role, mid, primary.model)
            return out[0]
    return None


def _native_model_chain(role: str, ep: Endpoint, payload: bytes,
                        timeout_s: int, *, meter: list) -> "dict | None":
    """The model chain for the NATIVE tool-calling path.

    Same rule as the text path — one attempt per configured model, own key and
    own TLS for a row that names its own host — but it rewrites the model id
    inside the already-built JSON body rather than rebuilding it, so the tool
    definitions and every other parameter travel unchanged.
    """
    if not _model_chain_enabled():
        return None
    try:
        from aiforge_core.config import model_registry
        rows = model_registry.chain_after(ep.model, ep.base_url)
    except Exception:  # noqa: BLE001
        return None
    try:
        body_obj = json.loads(payload.decode())
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("model") or "").strip()
        if not mid:
            continue
        _row_url = str(row.get("base_url") or "").strip()
        if _row_url and _row_url.rstrip("/") != (ep.base_url or "").rstrip("/"):
            _ex = dict(ep.extras or {})
            _ex.pop("insecure_tls", None)
            if row.get("insecure_tls"):
                _ex["insecure_tls"] = True
            alt = replace(ep, model=mid, base_url=_row_url,
                          api_key=str(row.get("api_key") or ""), extras=_ex)
        else:
            alt = replace(ep, model=mid)
        body_obj["model"] = mid
        _log.warning(
            "llm.model_chain_try role=%s failed=%s trying=%s endpoint=%s "
            "(native tool path)", role, ep.model, mid, alt.base_url)
        try:
            out = _post_with_retry(alt, json.dumps(body_obj).encode(),
                                   timeout_s, role=role,
                                   source="model_chain_native", meter=meter)
        except _LLMCancelled:
            raise
        except Exception:  # noqa: BLE001 — try the next configured model
            continue
        _log.warning("llm.model_chain_used role=%s model=%s (selected %s did "
                     "not answer)", role, mid, ep.model)
        return out
    return None


def _autofallback_enabled() -> bool:
    """Stand in for a missing model with one the endpoint serves.

    On by default: a box that serves SOMETHING can usually still do the work,
    and the alternative is every role failing until a human edits a config
    file. ``AIFORGE_LLM_MODEL_AUTOFALLBACK=0`` turns it off for an operator who
    would rather a wrong model be a hard failure — a fair position when the
    model choice is the experiment.
    """
    import os as _os
    return _os.environ.get("AIFORGE_LLM_MODEL_AUTOFALLBACK", "1") not in (
        "0", "false", "no")


def _looks_like_a_model_error(shipped: dict) -> bool:
    """Is it worth asking the endpoint which models it serves?

    Only when the box ANSWERED and its answer was about the model: a 4xx (LM
    Studio's "No models loaded", a 404 for an unknown id) or the reloading
    exception that wording raises. Two exclusions, both load-bearing:

    * A SHIPPED read timeout proves the model exists — it accepted the prompt
      and is still generating. Calling that a missing model would rename the
      one failure the whole no-re-POST rule is built around.
    * A refused connection, a DNS failure or an unreachable host says nothing
      about model configuration, and probing on every such failure means an
      outbound request from code paths (including tests) that never asked for
      one.
    """
    if shipped.get("timeout"):
        return False
    exc = shipped.get("exc")
    if exc is None:
        return False
    if isinstance(exc, _ModelReloading):
        return True
    return isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500


def _try_post(ep: Endpoint, messages: list[dict],
              *, temperature, max_tokens, top_p, extras,
              timeout_s: int, role: str,
              source: str,
              empty_retries: int | None = None,
              shipped: dict | None = None) -> tuple[str, dict] | None:
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
    empty_retries = (max(0, _int_env("AIFORGE_LLM_EMPTY_RETRIES", 3))
                     if empty_retries is None else max(0, empty_retries))
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
        _meter_tok: list = [None]
        try:
            body = _post_with_retry(ep, payload, timeout_s,
                                    role=role, source=source,
                                    meter=_meter_tok)
        except _LLMCancelled:
            raise
        except (OSError, ValueError) as _texc:
            # ValueError covers a non-JSON 200 (proxy HTML error page,
            # truncated / streaming body) so a malformed response falls back to
            # the next provider instead of crashing complete(). Transport
            # errors are NOT retried here — _post_with_retry already exhausted
            # its own transport retries; escalate to the next provider instead.
            #
            # Remember whether the prompt REACHED the model, though: this
            # returns None, and the exhausted-path RuntimeError below used to
            # discard the cause entirely, so the chat loop's own retry sweep
            # (AIFORGE_CHAT_LLM_RETRIES, default 5) re-issued the identical
            # completion five more times — six abandoned generations on a box
            # that could not finish one, which is the storm this whole change
            # is about.
            if shipped is not None and _shipped_timeout(_texc):
                shipped["timeout"] = True
            if shipped is not None:
                # Keep the LAST transport failure. What killed the chain
                # decides which diagnosis is even worth making: only a
                # model-lifecycle 4xx ("no models loaded", "model not found")
                # says anything about the configured model. A refused
                # connection or a read timeout does not.
                shipped["exc"] = _texc
            return None
        # The token from THIS attempt, so the cost lands on the minute and
        # turn that paid for it (same rule as a failure).
        _record_usage(role, body, _meter_tok[0])
        text = _extract_text(body)
        # "[]"/"{}" is a valid answer only for fast/structured roles (learner
        # etc.), never for conversational chat/doer output.
        if not _is_garbage(text, allow_empty_json=fast_role):
            return text, body
        # A 200-OK that carries no usable content is a FAILED request: it cost
        # a generation, it is about to be re-posted, and this loop is the
        # documented-common failure on self-hosted reasoning models. It raises
        # nothing, so the transport could not count it — count it here, with
        # the same `empty` label the ADK path uses (escalating_llm/_wrapper),
        # or the two meters give opposite verdicts about the same endpoint.
        _record_empty(_meter_tok[0])
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
            time.sleep(0.4 + _jitter.random() * 0.6)
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
    # The meter token, threaded exactly as `_try_post` does it. Without it the
    # tokens land machine-wide but on NO turn — and this is the DEFAULT chat
    # path (AIFORGE_CHAT_TOOL_PROTOCOL=native), so "how much did this message
    # write" read 0 for almost every real message while the session total
    # climbed. A per-turn number that is always zero is worse than none.
    _meter_tok: list = [None]
    try:
        body = _post_with_retry(ep, payload, timeout_s, role=role,
                                source="native", meter=_meter_tok)
    except _LLMCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — chain first, then re-raise
        # THE DEFAULT CHAT PATH. AIFORGE_CHAT_TOOL_PROTOCOL defaults to
        # "native", so simple chat comes through HERE, not through complete().
        # A fallback chain that only existed on the other path was a fallback
        # the user could never actually reach: "chat picks one model and if it
        # fails it should go to the others" is exactly this function.
        body = _native_model_chain(role, ep, payload, timeout_s,
                                   meter=_meter_tok)
        if body is None:
            raise exc
    _record_usage(role, body, _meter_tok[0])
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
    # The role's OWN endpoint, kept before any escalation rebinds `primary`.
    _chain_base = primary
    _escalated_for_overflow = False

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
        _escalated_for_overflow = True

    # Attempt 1 — primary
    _shipped: dict = {}
    out = _try_post(primary, messages, shipped=_shipped,
                    temperature=temperature, max_tokens=max_tokens,
                    top_p=top_p, extras=extras,
                    timeout_s=timeout_s, role=role, source="primary")
    if out is not None:
        return out[0]

    # Attempt 2 — fallback() (different provider, same role)
    fb = fallback(role)
    if fb is not None and fb.provider != primary.provider:
        out = _try_post(fb, messages, shipped=_shipped,
                        temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role, source="fallback")
        if out is not None:
            return out[0]

    # Attempt 3 — escalate on quality (forces cloud regardless of ctx)
    cloud = escalate(role, reason="quality")
    if cloud is not None and cloud.provider != primary.provider:
        out = _try_post(cloud, messages, shipped=_shipped,
                        temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p, extras=extras,
                        timeout_s=timeout_s, role=role,
                        source="quality_escalation")
        if out is not None:
            return out[0]

    # THE OTHER MODELS THE OPERATOR CONFIGURED. "I added four models; when the
    # one chat picked stops answering it should try the others" — until now the
    # registry was a selection list only, and the provider fallback chain is
    # for CLOUD escalation (empty without a cloud key), so on a single-provider
    # install a dead model was simply the end.
    #
    # Deliberately AFTER the same-provider fallback and the quality escalation,
    # and one attempt per model: this is a rescue, not a routing policy. Each
    # switch is logged, because an answer that silently came from a different
    # model than the operator selected is worse than a clear failure.
    # Chain off the endpoint the ROLE is configured with, not off `primary`:
    # a context_overflow escalation rebinds `primary` to the cloud, and
    # chaining from there would offer the failed local model back to itself,
    # send local model ids (with the cloud key) to the vendor, and re-send a
    # prompt that was escalated precisely because it does not fit locally.
    # DIAGNOSE FIRST, rescue second. Run before the chain so a role pointed at
    # a model id the endpoint does not serve is REPORTED even when another
    # configured model then answers: otherwise every turn is quietly served by
    # a model the operator did not select, at WARNING level only, forever.
    _missing_now = None
    if _looks_like_a_model_error(_shipped):
        try:
            from ._models import model_is_missing as _mim0
            _missing_now = _mim0(_chain_base.base_url, _chain_base.model,
                                 _chain_base.api_key or "")
        except Exception:  # noqa: BLE001
            _missing_now = None
    if _missing_now:
        _log.error(
            "llm.model_missing role=%s model=%s endpoint=%s available=%s — "
            "CONFIGURATION: that model is not served here. Fix the role's "
            "model or load it; a fallback answering in its place is a rescue, "
            "not a fix.",
            role, _chain_base.model, _chain_base.base_url,
            ", ".join(_missing_now[:8]),
            extra={"aiforge": {"role": role, "model": _chain_base.model,
                               "endpoint": _chain_base.base_url,
                               "available": _missing_now}},
        )
    _chain_tried = 0
    if _escalated_for_overflow:
        out = None
    else:
        _tried: list = []
        out = _try_model_chain(role, _chain_base, messages,
                               temperature=temperature, max_tokens=max_tokens,
                               top_p=top_p, extras=extras, timeout_s=timeout_s,
                               shipped=_shipped, tried=_tried)
        _chain_tried = len(_tried)
    if out is not None:
        return out

    # Before blaming the network: is the configured model even served here? A
    # role pointed at a model id the box does not have fails with model-
    # lifecycle wording ("No models loaded"), which reads as transient, so
    # every layer retries a permanent config error — and the user is told "the
    # model didn't respond", naming neither the model nor the endpoint. One
    # cheap GET turns that into the sentence that fixes it.
    _missing = None
    if _looks_like_a_model_error(_shipped):
        try:
            from ._models import model_is_missing as _mim
            _missing = _mim(primary.base_url, primary.model,
                            primary.api_key or "")
        except Exception:  # noqa: BLE001 — a diagnostic must never mask the error
            _missing = None
    if _missing:
        # The box told us what it DOES serve — so use it rather than failing a
        # whole run over one stale line of config. One retry, against the
        # closest id the endpoint actually has. This is a rescue, not a
        # routing decision: it is logged loudly at WARNING every time, because
        # a silent substitution means the operator never learns their config
        # is wrong and quietly gets a different model than they think.
        _sub = None
        if _autofallback_enabled():
            try:
                from ._models import pick_substitute as _pick
                _sub = _pick(primary.model, _missing)
            except Exception:  # noqa: BLE001
                _sub = None
        if _sub:
            _log.warning(
                "llm.model_substituted role=%s configured=%s served=%s "
                "using=%s endpoint=%s — the configured model is not served "
                "here; fix the role config or load it",
                role, primary.model, ",".join(_missing[:8]), _sub,
                primary.base_url,
                extra={"aiforge": {"role": role, "configured": primary.model,
                                   "using": _sub, "available": _missing,
                                   "endpoint": primary.base_url}},
            )
            _subbed = replace(primary, model=_sub)
            out = _try_post(_subbed, messages, shipped={},
                            temperature=temperature, max_tokens=max_tokens,
                            top_p=top_p, extras=extras, timeout_s=timeout_s,
                            role=role, source="model_substitute")
            if out is not None:
                return out[0]
    if _missing is not None:
        _have = ", ".join(_missing[:8]) if _missing else "none loaded"
        _exhausted = RuntimeError(
            f"llm.model_missing role={role} model={primary.model} "
            f"endpoint={primary.base_url} — that model is NOT served here "
            f"(available: {_have}). This is configuration, not a transport "
            f"failure: point the role at one of the models above, or load it "
            f"on the endpoint. Retrying cannot fix it."
        )
        setattr(_exhausted, MODEL_MISSING_ATTR, True)
        _log.error(
            "llm.model_missing role=%s model=%s endpoint=%s available=%s",
            role, primary.model, primary.base_url, _have,
            extra={"aiforge": {"role": role, "model": primary.model,
                               "endpoint": primary.base_url,
                               "available": _missing}},
        )
        raise _exhausted
    _exhausted = RuntimeError(
        f"llm.exhausted role={role} primary={primary.provider}"
        f"@{primary.base_url} model={primary.model} "
        f"fallback={fb.provider if fb else 'none'} "
        f"cloud={cloud.provider if cloud else 'none'} "
        f"— all providers returned transport error or empty content "
        f"(see the llm.transport_error line above for the underlying cause)"
        + (f"; also tried {_chain_tried} configured model(s) from the registry"
           if _chain_tried else "")
    )
    if _shipped.get("timeout"):
        # The prompt DID reach the model — callers above must not re-issue it.
        setattr(_exhausted, _TIMEOUT_SHIPPED_ATTR, True)
    raise _exhausted
