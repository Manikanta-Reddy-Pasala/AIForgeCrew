"""One attempt against one endpoint: the request body for that attempt, the
post itself with its transport retries, and what a failed attempt records."""
from __future__ import annotations

import os
import random
import time

from ..types import Endpoint
from ._errors import (
    _LLMCancelled,
)
from ._helpers import _int_env
from ._http import shipped_timeout as _shipped_timeout
from ._text import (
    _append_no_think,
    _extract_text,
    _is_garbage,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm.client as package
    return package


_jitter = random.SystemRandom()


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


def _attempt_payload(ep: Endpoint, messages: list[dict], attempt: int,
                     fast_role: bool, temperature, max_tokens, top_p, extras):
    """The request body for one empty-retry attempt.

    Attempt 0: coax /no_think for fast/direct-output roles (a reasoning model
    would otherwise return EMPTY for a role that wants a plain answer). Later
    attempts: the last post came back EMPTY, so append '/no_think' (Qwen/DeepSeek
    honour it → skip the reasoning phase) and PROGRESSIVELY widen max_tokens
    (×2, ×3, …) so a still-thinking model has room left to emit the answer.
    """
    extras = _fast_extras(ep, fast_role, extras)
    if attempt == 0:
        base = _append_no_think(messages) if fast_role else messages
        return _pkg()._build_body(ep, base, temperature, max_tokens, top_p, extras)
    mt = min(int((max_tokens or 4096) * (attempt + 1)), 32768)
    return _pkg()._build_body(ep, _append_no_think(messages), temperature, mt,
                       top_p, extras)


def _fast_extras(ep: Endpoint, fast_role: bool, extras):
    """A fast role also asks the server to skip reasoning outright (see
    :mod:`aiforge_core.llm.fast_reasoning`); a caller's own extras win."""
    try:
        from aiforge_core.llm import fast_reasoning
        add = fast_reasoning.extras_for(ep.base_url, fast_role)
    except Exception:  # noqa: BLE001 — never break a call over this
        add = {}
    if not add:
        return extras
    return {**add, **(extras or {})}


def _rejected_fast_extras(ep: Endpoint, fast_role: bool, exc) -> bool:
    """The server refused the reasoning field: remember it, re-send without."""
    if not fast_role:
        return False
    try:
        from aiforge_core.llm import fast_reasoning
        return fast_reasoning.note_rejection(ep.base_url, exc)
    except Exception:  # noqa: BLE001
        return False


def _note_transport_failure(shipped: "dict | None", exc: Exception) -> None:
    """Record a transport failure's diagnosis on ``shipped`` so the exhausted
    path can reason about it. Whether the prompt REACHED the model decides which
    diagnosis is worth making: only a model-lifecycle 4xx ("no models loaded",
    "model not found") says anything about the configured model; a refused
    connection or a read timeout does not — and a shipped timeout must never be
    re-issued."""
    if shipped is None:
        return
    if _shipped_timeout(exc):
        shipped["timeout"] = True
    shipped["exc"] = exc


def _post_fast_aware(ep, payload_fn, timeout_s, role, source, meter,
                     fast_role):
    """One post. When the server refuses the fast-role reasoning field, it is
    remembered and the same request goes once more without it."""
    try:
        return _pkg()._post_with_retry(ep, payload_fn(), timeout_s, role=role,
                                       source=source, meter=meter)
    except _LLMCancelled:
        raise
    except OSError as exc:
        if not _rejected_fast_extras(ep, fast_role, exc):
            raise
    return _pkg()._post_with_retry(ep, payload_fn(), timeout_s, role=role,
                                   source=source, meter=meter)


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
    reasoning models: the same prompt re-issued to the SAME endpoint usually
    returns real content. With a single-model setup there is no fallback
    provider, so retrying here is the only thing that turns a dropped learner
    capture or a stalled generation back into a real answer. Retry count is
    AIFORGE_LLM_EMPTY_RETRIES (default 2 → up to 3 total posts).
    """
    empty_retries = (max(0, _int_env("AIFORGE_LLM_EMPTY_RETRIES", 3))
                     if empty_retries is None else max(0, empty_retries))
    fast_role = _is_fast_role(role)
    for attempt in range(empty_retries + 1):
        def _payload(_a=attempt):
            return _attempt_payload(ep, messages, _a, fast_role,
                                    temperature, max_tokens, top_p, extras)
        _meter_tok: list = [None]
        try:
            body = _post_fast_aware(ep, _payload, timeout_s, role, source,
                                    _meter_tok, fast_role)
        except _LLMCancelled:
            raise
        except OSError as _texc:
            _note_transport_failure(shipped, _texc)
            return None
        except ValueError as _texc:
            # ValueError covers a non-JSON 200 (proxy HTML error page, truncated
            # / streaming body) so a malformed response falls back to the next
            # provider instead of crashing complete(). Transport errors are NOT
            # retried here — _post_with_retry already exhausted its own; escalate
            # to the next provider instead.
            _note_transport_failure(shipped, _texc)
            return None
        # The token from THIS attempt, so the cost lands on the minute and turn
        # that paid for it (same rule as a failure).
        _pkg()._record_usage(role, body, _meter_tok[0])
        text = _extract_text(body)
        # "[]"/"{}" is a valid answer only for fast/structured roles (learner
        # etc.), never for conversational chat/doer output.
        if not _is_garbage(text, allow_empty_json=fast_role):
            return text, body
        # A 200-OK that carries no usable content is a FAILED request: it cost a
        # generation, it is about to be re-posted, and it raises nothing, so the
        # transport could not count it — count it here with the same `empty`
        # label the ADK path uses, or the two meters disagree about the endpoint.
        _record_empty(_meter_tok[0])
        _pkg()._log.warning(
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
