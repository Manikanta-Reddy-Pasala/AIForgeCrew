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


# Marker attribute set on an exception whose prompt REACHED the model and was
# abandoned on a read timeout. Callers above the transport (the chat loop's own
# retry sweep) must not re-issue that completion: each attempt leaves another
# generation running on a box that already could not finish the first.
TIMEOUT_SHIPPED_ATTR = "aiforge_llm_timeout_shipped"


def shipped_timeout(exc: BaseException) -> bool:
    """True when ``exc`` came from a request the model actually received."""
    return bool(getattr(exc, TIMEOUT_SHIPPED_ATTR, False))


# Floor under the retry budget, so the deadline rule only ever bites callers
# whose per-attempt timeout is ALREADY long. A 1s health probe failing on a
# refused connection still gets its (free, instant) retries.
_RETRY_MIN_BUDGET_S = 10.0

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
                      cancel, sent: "list | None" = None) -> dict:
    """POST via http.client so a watcher thread can close the connection the
    instant ``cancel`` fires — interrupting an otherwise-blocking generation.
    Used only when a cancel token is bound for this thread."""
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
        # The prompt is now the SERVER's problem — everything after this point
        # is waiting for it to answer, and a retry would duplicate work it is
        # already doing. Everything BEFORE it (connect, TLS handshake, send)
        # cost the server nothing, so those failures stay retryable even when
        # they surface as a bare TimeoutError from http.client.
        if sent is not None:
            sent[0] = True
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


def _record_request(role: str | None = None, provider: str | None = None,
                    model: str | None = None) -> None:
    """Count one request to the model. Called at the moment we are actually
    about to send — AFTER the rate-limit wait and the preflight, so an attempt
    that never reached the network is not reported as provider traffic (the
    meter exists to answer "why did one question cost forty calls", and a
    sleeping local box must not manufacture forty).

    ``role`` is threaded down from _post_with_retry rather than left to the
    request context: the background daemon (compaction, folds, jobs) has no
    request context at all, and its calls are precisely the ones the toolbar
    meter exists to make visible."""
    try:
        from aiforge_core.llm import call_meter as _meter
        _meter.record(role=role, provider=provider, model=model)
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass


def _post(ep: Endpoint, payload: bytes, timeout_s: int,
          *, role: str | None = None, sent: "list | None" = None,
          max_wait_s: float | None = None,
          throttled: "list | None" = None) -> dict:
    # Rate-limit acquire BEFORE the post — blocks until budget allows.
    prov = _providers.get(ep.provider)
    declared = prov.rate_limits() if prov is not None else None
    # The rate-limit wait is time the CALLER spends inside this attempt, so it
    # is bounded by whatever is left of the caller's budget. Sizing it only by
    # AIFORGE_LLM_MAX_WAIT_S (120s) meant a 15s classifier could legitimately
    # block for over two minutes inside a chain the log called a 25s budget.
    _wait_cap = float(_int_env("AIFORGE_LLM_MAX_WAIT_S", 120))
    if max_wait_s is not None:
        _wait_cap = max(1.0, min(_wait_cap, max_wait_s))
    _rl.acquire(
        ep.provider,
        declared=declared,
        tokens_estimate=_estimate_tokens(payload),
        max_wait_s=_wait_cap,
    )
    cancel = _CANCEL.get()
    # ONE preflight for both paths, BEFORE the meter. It used to sit inside
    # _post_cancellable, so the cancellable path (which is every chat
    # generation) counted a request that the preflight then proved could not
    # be sent: against a sleeping box the toolbar read "18 requests · 18/min"
    # with zero bytes on the wire — the meter inventing the overload it exists
    # to diagnose, in the one situation someone is staring at it.
    if cancel is None or not cancel.is_set():
        _preflight(ep.base_url)
    # The OPERATOR's ceiling waits HERE — after the cancel check and the
    # preflight, immediately before the request is counted and sent. Waiting
    # earlier spent the ceiling's budget on calls that never left the box (a
    # sleeping endpoint drained the whole minute), made Stop unable to
    # interrupt a parked call, and delayed the preflight whose entire job is to
    # fail an unreachable endpoint fast. Its wait is deliberately NOT bounded
    # by the caller's retry budget — a queue is not a failure, and charging it
    # there turned "you are throttled" into "your classifier errored".
    _throttled = _rl.acquire_global(
        max_wait_s=float(_int_env("AIFORGE_LLM_MAX_WAIT_S", 120)))
    if throttled is not None:
        throttled[0] = _throttled
    if cancel is not None:
        _record_request(role, ep.provider, ep.model)
        return _post_cancellable(ep, payload, timeout_s, cancel, sent)
    _record_request(role, ep.provider, ep.model)
    # urllib wraps connect/handshake/send failures in URLError, so a bare
    # TimeoutError out of urlopen is a READ timeout — the server has the
    # prompt. Marking here is therefore exact for this path.
    if sent is not None:
        sent[0] = True
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
      AIFORGE_LLM_RETRY_BUDGET  — retries must fit timeout_s x THIS (default
                                  1.5); 0 disables the budget entirely
      AIFORGE_LLM_RETRY_TIMEOUT_MAX — attempts allowed when the failure is a
                                  READ TIMEOUT (default 1 = do not re-POST)

    On 429 with Retry-After, honour the header (capped to retry_cap).
    Permanent (4xx non-429) errors bubble immediately.

    A READ TIMEOUT IS NOT RETRIED by default — but only a real one. It is the
    single transient failure meaning the server ACCEPTED the request and is
    still working on it: re-POSTing leaves the first generation running and
    adds a second, so the retry worsens the overload it is retrying on. The
    rule is gated on the request having actually SHIPPED (``sent``), because a
    bare TimeoutError also comes out of the client-side rate limiter giving up
    and out of a stalled connect/TLS handshake — neither of which cost the
    server anything, and both of which must keep their retries. Set
    ``AIFORGE_LLM_RETRY_TIMEOUT_MAX`` above 1 to re-POST anyway.

    Be aware of what does NOT stand behind this: in the default self-hosted
    setup ``router._CLOUD_PROVIDERS`` is empty and ``fallback()`` has no second
    provider to offer, so a read timeout ends ``complete()`` with
    ``llm.exhausted`` rather than falling through to another endpoint. That is
    the deliberate trade — one abandoned generation beats three.

    THE BUDGET. ``timeout_s`` is the caller's deadline, not one attempt's: a
    20s route classifier retrying three read-timeouts blocks its caller for a
    full minute. So the chain is bounded by ``timeout_s * budget_mult``
    (floored at +10s so short-timeout callers keep their cheap retries) and a
    retry is made only when a FULL attempt still fits inside what is left —
    never a stub with a few seconds on it, which would manufacture exactly the
    abandoned generation this is written to avoid. The bound is per CHAIN;
    ``client.complete``'s empty-response loop can start several."""
    max_attempts = max(1, _int_env("AIFORGE_LLM_RETRY_MAX", 3))
    base = _float_env("AIFORGE_LLM_RETRY_BASE_S", 0.5)
    cap = _float_env("AIFORGE_LLM_RETRY_CAP_S", 8.0)
    budget_mult = _float_env("AIFORGE_LLM_RETRY_BUDGET", 1.5)
    timeout_max = max(1, _int_env("AIFORGE_LLM_RETRY_TIMEOUT_MAX", 1))
    started = time.monotonic()
    budget_s = max(max(1.0, timeout_s) * budget_mult,
                   timeout_s + _RETRY_MIN_BUDGET_S)
    deadline = started + budget_s if budget_mult > 0 else None
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        # Did THIS attempt get the prompt onto the wire? Decides whether a
        # timeout means "the server is working on it" or "we never reached it".
        sent = [False]
        try:
            _left = (deadline - time.monotonic()) if deadline is not None else None
            _throttled = [0.0]
            return _post(ep, payload, timeout_s, role=role, sent=sent,
                         max_wait_s=_left, throttled=_throttled)
        except Exception as exc:  # noqa: BLE001 — classifier handles
            retry, label = _is_transient_exc(exc)
            last = exc
            # Time spent QUEUED on the operator's ceiling is not time this
            # attempt spent failing, so it must not eat the retry budget: a
            # throttled call would otherwise arrive at the retry check with its
            # deadline already gone and lose retries it used to get for free.
            if deadline is not None and _throttled[0] > 0:
                deadline += _throttled[0]
            # Cost the NEXT attempt before deciding to make it — the backoff is
            # part of the caller's deadline too, and a 429 Retry-After can be
            # minutes on its own.
            sleep_s = min(cap, base * (2 ** (attempt - 1)))
            ra: str | None = None
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                except Exception:  # noqa: BLE001
                    ra = None
            if ra:
                try:
                    sleep_s = min(cap, max(0.1, float(ra)))
                except ValueError:
                    pass
            # Add jitter to avoid thundering herd against shared providers.
            sleep_s += random.uniform(0, 0.25)
            budget_out = False
            if (retry and label == "timeout" and sent[0]
                    and attempt < max_attempts and attempt >= timeout_max):
                budget_out = True
                _log.info(
                    "llm.timeout_not_retried provider=%s attempt=%d "
                    "timeout=%ds — the server already has this request",
                    ep.provider, attempt, timeout_s,
                    extra={"aiforge": {"role": role, "provider": ep.provider,
                                       "source": source, "attempt": attempt,
                                       "label": label,
                                       "error": str(exc)[:200]}},
                )
            if (not budget_out and retry and deadline is not None
                    and attempt < max_attempts):
                # A retry gets the FULL per-attempt timeout or it is not made.
                if time.monotonic() + sleep_s + float(timeout_s) > deadline:
                    budget_out = True
                    _log.info(
                        "llm.retry_budget_exhausted provider=%s label=%s "
                        "attempt=%d elapsed=%.1fs budget=%.1fs — not retrying",
                        ep.provider, label, attempt,
                        time.monotonic() - started, budget_s,
                        extra={"aiforge": {"role": role, "provider": ep.provider,
                                           "source": source, "attempt": attempt,
                                           "label": label,
                                           "sleep_s": round(sleep_s, 3),
                                           "error": str(exc)[:200]}},
                    )
            if not retry or budget_out or attempt >= max_attempts:
                if label == "timeout" and sent[0]:
                    try:
                        setattr(exc, TIMEOUT_SHIPPED_ATTR, True)
                    except Exception:  # noqa: BLE001 — never break on a marker
                        pass
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
                                       "budget_exhausted": budget_out,
                                       "error": (str(exc) + " " + _body)[:300]}},
                )
                raise
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
