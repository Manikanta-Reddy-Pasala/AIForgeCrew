"""HTTP transport for the LLM client: cancel token, request-body building,
the (default urllib + opt-in cancellable http.client) POST paths, the
connect-preflight, and the bounded transient-retry wrapper.

Layers on the leaf helpers (:mod:`._helpers`, :mod:`._errors`) plus the sibling
``providers`` / ``rate_limiter`` / ``_ssl`` modules of ``aiforge_core.llm``."""
from __future__ import annotations

import contextvars
import io
import json
import os
import random
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
from ..types import Endpoint
from ._errors import (
    _http_err_body,
    _is_transient_exc,
    _LLMCancelled,
    _raise_if_model_dropped,
)
from ._helpers import _estimate_tokens, _float_env, _int_env, _log

# Optional per-thread cancel token. When a caller (the chat agent's Stop path)
# sets it on the thread that runs ``complete``, ``_post`` uses an interruptible
# HTTP path that closes the connection the instant the event fires — so Stop
# can abort an in-flight generation instead of waiting it out. Unset (the
# default for every other caller) → the normal urllib path, byte-identical.
_CANCEL: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_llm_cancel", default=None)


def set_cancel_event(ev) -> None:
    """Bind a threading.Event as the cancel token for THIS thread's LLM call."""
    _CANCEL.set(ev)


# Endpoint.extras keys that are transport/routing control — never sent as
# OpenAI chat-completion body params (strict servers 400 on unknown keys).
_NON_BODY_EXTRA_KEYS = frozenset({"insecure_tls"})


def _build_body(ep: Endpoint, messages: list[dict],
                temperature: float | None,
                max_tokens: int | None,
                top_p: float | None,
                extras: dict | None) -> bytes:
    body: dict = {
        "model": ep.model,
        "messages": messages,
    }
    # When the caller didn't pin a temperature, honour a model-keyed forced
    # temperature from the quirk sheet (e.g. qwythos -> 0.0). This is the
    # only path the direct client.complete callers (enhancer / architect /
    # decompose) take — EscalatingLlm applies the same sheet separately.
    if temperature is None:
        try:
            from aiforge_core.config import model_overrides as _mo
            _ov = _mo.lookup(ep.model)
            if _ov and _ov.get("temperature") is not None:
                temperature = _ov["temperature"]
        except Exception:  # noqa: BLE001 — overrides must never break a call
            pass
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if top_p is not None:
        body["top_p"] = top_p
    # Provider-bundled extras first, then per-call extras override. Strip
    # transport-control keys (TLS opt-out) — they live on the Endpoint for
    # _post, NOT as chat-completion body params. Leaking insecure_tls into
    # the body makes strict servers (e.g. Open WebUI) reject with HTTP 400.
    body.update({k: v for k, v in ep.extras.items()
                 if k not in _NON_BODY_EXTRA_KEYS})
    if extras:
        body.update(extras)
    # Strict OpenAI-compatible servers (LM Studio, and the operator's
    # self-hosted proxy) reject response_format.type=json_object — they
    # accept only json_schema or text. openai_compatible is the only
    # provider now, so always normalise json_object → a permissive
    # json_schema. (Real OpenAI accepts json_schema too, so this is safe.)
    if ep.provider == "openai_compatible":
        rf = body.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "out",
                    "schema": {"type": "object"},
                    "strict": False,
                },
            }
    return json.dumps(body).encode()


def _post_headers(ep: Endpoint) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ep.api_key}",
        # Some proxies/WAFs reject the stdlib Python-urllib UA.
        "User-Agent": os.environ.get(
            "AIFORGE_LLM_USER_AGENT", "curl/8.5.0 (aiforge)"),
    }


def _post_ctx(ep: Endpoint):
    # Per-endpoint TLS context. Skip verification when the role carries the
    # explicit insecure_tls opt-out OR the host is trusted-internal (self-
    # hosted LAN box, self-signed is normal). Public hosts verify; a CA
    # bundle keeps verify ON. Otherwise honour AIFORGE_LLM_SSL_VERIFY / CA.
    base = ep.base_url
    insecure = bool((ep.extras or {}).get("insecure_tls"))
    if str(base).lower().startswith("https://") and (
        insecure or _ssl_auto_relax(base)
    ) and not _ssl_ca_bundle():
        return _ssl_insecure()
    return _ssl_context_for(base)


def _post_cancellable(ep: Endpoint, payload: bytes, timeout_s: int,
                      cancel) -> dict:
    """POST via http.client so a watcher thread can close the connection the
    instant ``cancel`` fires — interrupting an otherwise-blocking generation.
    Used only when a cancel token is bound for this thread."""
    if not cancel.is_set():
        _preflight(ep.base_url)   # skip if already cancelled → abort below
    import http.client
    from urllib.parse import urlparse
    url = f"{ep.base_url.rstrip('/')}/chat/completions"
    p = urlparse(url)
    host, port = p.hostname, (p.port or (443 if p.scheme == "https" else 80))
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout_s,
                                           context=_post_ctx(ep))
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    stop = threading.Event()

    def _watch():
        while not stop.wait(0.15):
            if cancel.is_set():
                try:
                    conn.close()   # unblocks getresponse() on the main thread
                except Exception:  # noqa: BLE001
                    pass
                return
    threading.Thread(target=_watch, daemon=True).start()
    try:
        if cancel.is_set():
            raise _LLMCancelled("cancelled before request")
        conn.request("POST", path, body=payload, headers=_post_headers(ep))
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            # Mimic urllib's HTTPError so the retry classifier handles it the
            # same way (5xx/429 retry, other 4xx permanent).
            raise urllib.error.HTTPError(
                url, resp.status, resp.reason, resp.headers, io.BytesIO(data))
        _body = json.loads(data)
        _raise_if_model_dropped(_body)   # 200-OK error body → transient
        return _body
    except (http.client.HTTPException, OSError) as exc:
        if cancel.is_set():
            raise _LLMCancelled("cancelled mid-request") from exc
        raise
    finally:
        stop.set()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _post(ep: Endpoint, payload: bytes, timeout_s: int) -> dict:
    # Count the REQUEST here — one per HTTP attempt, so retries, fallbacks and
    # escalations each count, exactly as the provider's rate limiter counts
    # them. This is the number the chat UI shows when someone asks why a single
    # question turned into forty calls.
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record()
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass
    # Rate-limit acquire BEFORE the post — blocks until budget allows.
    prov = _providers.get(ep.provider)
    declared = prov.rate_limits() if prov is not None else None
    _rl.acquire(
        ep.provider,
        declared=declared,
        tokens_estimate=_estimate_tokens(payload),
        max_wait_s=float(_int_env("AIFORGE_LLM_MAX_WAIT_S", 120)),
    )
    cancel = _CANCEL.get()
    if cancel is not None:
        return _post_cancellable(ep, payload, timeout_s, cancel)
    _preflight(ep.base_url)
    req = urllib.request.Request(
        f"{ep.base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers=_post_headers(ep),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s,
                                context=_post_ctx(ep)) as resp:
        _body = json.loads(resp.read())
        _raise_if_model_dropped(_body)   # 200-OK error body → transient
        return _body


def _preflight(base_url: str) -> None:
    """Fast TCP reachability check before a chat completion. urllib/http.client
    apply a single scalar timeout to BOTH connect and read, so an unreachable
    or asleep host (dropped SYN, no RST) blocks the FULL request timeout
    (chat default 600s) just to fail the TCP connect — the simple-chat
    equivalent of the pipeline retry-storm. A short connect probe fails an
    unreachable endpoint in seconds instead. Reuses the same
    AIFORGE_LLM_CONNECT_TIMEOUT_S knob as the pipeline (escalating_llm).
    ``0`` disables the preflight. Raises ConnectionError when unreachable."""
    ct = _float_env("AIFORGE_LLM_CONNECT_TIMEOUT_S", 8.0)
    if ct <= 0:
        return
    import socket as _socket
    from urllib.parse import urlparse as _urlparse
    try:
        u = _urlparse(base_url)
        host = u.hostname
        if not host:
            return
        port = u.port or (443 if u.scheme == "https" else 80)
    except Exception:  # noqa: BLE001 — malformed url → let the real call surface it
        return
    try:
        _socket.create_connection((host, port), timeout=ct).close()
    except OSError as exc:
        raise ConnectionError(
            f"LLM endpoint unreachable ({host}:{port}) within {ct:g}s "
            f"connect budget: {exc}") from exc


def _post_with_retry(ep: Endpoint, payload: bytes, timeout_s: int,
                     *, role: str, source: str) -> dict:
    """Wrap _post with bounded exponential backoff on transient errors.

    Knobs:
      AIFORGE_LLM_RETRY_MAX     — total attempts per endpoint (default 3)
      AIFORGE_LLM_RETRY_BASE_S  — base backoff seconds (default 0.5)
      AIFORGE_LLM_RETRY_CAP_S   — backoff cap seconds (default 8.0)

    On 429 with Retry-After, honour the header (capped to retry_cap).
    Permanent (4xx non-429) errors bubble immediately."""
    max_attempts = max(1, _int_env("AIFORGE_LLM_RETRY_MAX", 3))
    base = _float_env("AIFORGE_LLM_RETRY_BASE_S", 0.5)
    cap = _float_env("AIFORGE_LLM_RETRY_CAP_S", 8.0)
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _post(ep, payload, timeout_s)
        except Exception as exc:  # noqa: BLE001 — classifier handles
            retry, label = _is_transient_exc(exc)
            last = exc
            if not retry or attempt >= max_attempts:
                _body = _http_err_body(exc)
                _log.warning(
                    "llm.transport_error role=%s provider=%s model=%s "
                    "url=%s/chat/completions label=%s attempt=%d err=%s%s",
                    role, ep.provider, ep.model,
                    str(ep.base_url).rstrip("/"), label, attempt,
                    str(exc)[:300],
                    f" body={_body}" if _body else "",
                    extra={"aiforge": {"role": role, "provider": ep.provider,
                                       "model": ep.model, "source": source,
                                       "attempt": attempt, "label": label,
                                       "fatal": not retry,
                                       "error": (str(exc) + " " + _body)[:300]}},
                )
                raise
            # Honour Retry-After header for 429 if present + parseable.
            sleep_s = min(cap, base * (2 ** (attempt - 1)))
            ra: str | None = None
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                except Exception:
                    ra = None
            if ra:
                try:
                    sleep_s = min(cap, max(0.1, float(ra)))
                except ValueError:
                    pass
            # Add jitter to avoid thundering herd against shared providers.
            sleep_s += random.uniform(0, 0.25)
            _log.info(
                "llm.transport_retry provider=%s url=%s label=%s attempt=%d "
                "sleep=%.2fs err=%s",
                ep.provider, str(ep.base_url).rstrip("/"), label, attempt,
                sleep_s, str(exc)[:300],
                extra={"aiforge": {"role": role, "provider": ep.provider,
                                   "source": source, "attempt": attempt,
                                   "label": label,
                                   "sleep_s": round(sleep_s, 3),
                                   "error": str(exc)[:200]}},
            )
            time.sleep(sleep_s)
    # Defensive — loop above always either returns or raises.
    assert last is not None
    raise last
