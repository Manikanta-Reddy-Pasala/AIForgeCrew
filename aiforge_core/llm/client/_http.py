"""HTTP transport for the LLM client: cancel token, request-body building,
the (default urllib + opt-in cancellable http.client) POST paths, the
connect-preflight. The streaming reader lives in :mod:`._http_stream` and the
bounded transient-retry wrapper in :mod:`._http_retry`; both are re-exported.

Layers on the leaf helpers (:mod:`._helpers`, :mod:`._errors`) plus the sibling
``providers`` / ``rate_limiter`` / ``_ssl`` modules of ``aiforge_core.llm``."""
from __future__ import annotations

import contextvars
import json
import random  # noqa: F401  # tests patch _http.random
import threading
import time  # noqa: F401  # tests patch _http.time
import urllib.error
import urllib.request

from .. import providers as _providers
from .. import rate_limiter as _rl
from .._ssl import _ca_bundle as _ssl_ca_bundle
from .._ssl import auto_relax_internal as _ssl_auto_relax
from .._ssl import context_for as _ssl_context_for
from .._ssl import insecure_context as _ssl_insecure
from ..types import Endpoint
from ..user_agent import user_agent as _user_agent
from ._errors import (
    _is_transient_exc,
    _LLMCancelled,
    _raise_if_model_dropped,
)
from ._helpers import _estimate_tokens, _float_env, _int_env, _log
from ._http_retry import (  # noqa: F401  # re-exported
    _budget_exhausted,
    _log_budget_exhausted,
    _log_timeout_not_retried,
    _log_transport_error,
    _log_transport_retry,
    _mark_shipped_timeout,
    _next_sleep,
    _post_with_retry,
    _rate_limited_sleep,
    _retry_after_s,
    _RetryCfg,
    _timeout_already_shipped,
)
from ._http_stream import (  # noqa: F401  # re-exported
    _NO_STREAM,
    TIMEOUT_SHIPPED_ATTR,
    _pump_sse,
    _read_http_response,
    _read_sse_response,
    _StreamAssembler,
    _streaming_payload,
)

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


# Optional per-thread sink for STREAMED tokens: ``sink(kind, text)`` with kind
# "start" (a response began — a retry restarts the text), "content" or
# "reasoning". Bound by the chat loop next to the cancel token, so
# only the cancellable path streams; the response is reassembled into the
# normal non-streamed body, so retries, metering and tool calls are unchanged.
# Unset → one blocking request, as before.
_DELTA_SINK: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_llm_delta_sink", default=None)


def set_delta_sink(fn) -> None:
    """Bind ``fn(kind, text)`` to receive this thread's streamed tokens."""
    _DELTA_SINK.set(fn)


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


def _apply_reasoning_off(ep: Endpoint, body: dict) -> None:
    """Reasoning switched off for this model (Models → Thinking: no, or
    AIFORGE_NO_REASONING=1): add both switches the Qwen/DeepSeek family honours
    — the chat-template kwarg and /no_think on the last user turn."""
    try:
        from aiforge_core.llm import reasoning as _reasoning
        if _reasoning.reasoning_off(ep.model, ep.base_url):
            from ._text import _append_no_think
            body["messages"] = _append_no_think(body["messages"])
            body.update(_reasoning.NO_THINK_KWARGS)
    except Exception:  # noqa: BLE001 — never break a call over this
        pass


def _build_body(ep: Endpoint, messages: list[dict],
                temperature: float | None,
                max_tokens: int | None,
                top_p: float | None,
                extras: dict | None) -> bytes:
    body: dict = {
        "model": ep.model,
        "messages": messages,
    }
    _apply_reasoning_off(ep, body)
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
        # Identifies the client, the build and the person — "curl/8.5.0
        # (aiforge)" identified none of them, and gateway logs could not tell
        # one user's traffic from another's. AIFORGE_LLM_USER_AGENT still
        # overrides for a proxy that insists on something specific (which is
        # the only reason the curl string existed).
        "User-Agent": _user_agent(),
    }


def _post_ctx(ep: Endpoint):
    # Per-endpoint TLS context. The explicit insecure_tls opt-out (or a
    # trusted-internal host, where self-signed is normal) selects the PINNED
    # context — verification stays on, anchored to that endpoint's own
    # certificate. Public hosts verify against the ordinary roots; a CA bundle
    # wins over both. Otherwise honour AIFORGE_LLM_SSL_VERIFY / CA.
    base = ep.base_url
    insecure = bool((ep.extras or {}).get("insecure_tls"))
    if str(base).lower().startswith("https://") and (
        insecure or _ssl_auto_relax(base)
    ) and not _ssl_ca_bundle():
        # Pass the URL: the opt-out pins THAT endpoint's certificate and keeps
        # verifying (net.trust) rather than dropping verification, so it has to
        # know which host it is talking to.
        return _ssl_insecure(base)
    return _ssl_context_for(base)


def _open_connection(ep: Endpoint, url: str, timeout_s: int):
    """An http.client connection for ``url`` honouring the endpoint's TLS
    context. Returns ``(conn, path)`` — ``path`` carries any query string."""
    import http.client
    from urllib.parse import urlparse
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
    return conn, path


def _cancel_watcher(conn, cancel, stop: "threading.Event") -> None:
    """Start a daemon that closes ``conn`` the instant ``cancel`` fires,
    unblocking ``getresponse()`` on the main thread."""
    def _watch():
        while not stop.wait(0.15):
            if cancel.is_set():
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                return
    threading.Thread(target=_watch, daemon=True).start()


def _post_cancellable(ep: Endpoint, payload: bytes, timeout_s: int,
                      cancel, sent: "list | None" = None,
                      stream: bool = True) -> dict:
    """POST via http.client so a watcher thread can close the connection the
    instant ``cancel`` fires — interrupting an otherwise-blocking generation.
    Used only when a cancel token is bound for this thread. Streams when a
    delta sink is bound too (see :func:`set_delta_sink`); a server that
    rejects the stream request (400) is asked once more without it."""
    sink = _DELTA_SINK.get() if stream and ep.base_url not in _NO_STREAM else None
    if sink is not None:
        try:
            return _post_cancellable_once(ep, _streaming_payload(payload),
                                          timeout_s, cancel, sent, sink)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            _NO_STREAM.add(ep.base_url)
            _log.info("llm stream refused (400) by %s — unstreamed from now on",
                      ep.base_url)
    return _post_cancellable_once(ep, payload, timeout_s, cancel, sent, None)


def _post_cancellable_once(ep: Endpoint, payload: bytes, timeout_s: int,
                           cancel, sent, sink) -> dict:
    url = f"{ep.base_url.rstrip('/')}/chat/completions"
    conn, path = _open_connection(ep, url, timeout_s)
    stop = threading.Event()
    _cancel_watcher(conn, cancel, stop)
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
        if sink is not None:
            return _read_sse_response(conn, url, sink)
        return _read_http_response(conn, url)
    except OSError as exc:
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
                    model: str | None = None):
    """Count one request to the model. Called at the moment we are actually
    about to send — AFTER the rate-limit wait and the preflight, so an attempt
    that never reached the network is not reported as provider traffic (the
    meter exists to answer "why did one question cost forty calls", and a
    sleeping local box must not manufacture forty).

    ``role`` is threaded down from _post_with_retry rather than left to the
    request context: the background daemon (compaction, folds, jobs) has no
    request context at all, and its calls are precisely the ones the toolbar
    meter exists to make visible.

    Returns the meter's token for this request — hand it to
    ``_record_failure`` if the attempt does not come back with an answer, so
    the failure is charged to the minute and the chat turn that SENT it rather
    than to whenever it finally gave up (a 600s read timeout is ten minutes
    late)."""
    try:
        from aiforge_core.llm import call_meter as _meter
        return _meter.record(role=role, provider=provider, model=model)
    except Exception:  # noqa: BLE001 — metering must never break a call
        return None


def _record_failure(token, exc: BaseException) -> None:
    """That request came back with no answer. Counted SEPARATELY from, not
    instead of, the request itself: the attempt was real traffic (the provider
    billed and rate-limited it, the retry storm it belongs to is the thing the
    meter exists to expose), and a rate that dropped failures would read its
    lowest exactly when the endpoint is down. What the reader needs is both
    numbers — "40/min, 38 failing" — plus the label saying which failure.

    Never raises, and never lets the classifier's own failure escape into the
    caller's error path."""
    if not token:
        # `record` returns None only when it could not count the SEND. Counting
        # the failure anyway would put `failed` above `total` and paint a
        # failed-only minute the sparkline has no send to scale against.
        return
    try:
        from aiforge_core.llm import call_meter as _meter
        try:
            reason = _is_transient_exc(exc)[1] if isinstance(exc, Exception) \
                else exc.__class__.__name__
        except Exception:  # noqa: BLE001
            reason = exc.__class__.__name__
        _meter.record_failure(token, reason)
    except Exception:  # noqa: BLE001 — metering must never break a call
        pass


def _post(ep: Endpoint, payload: bytes, timeout_s: int,
          *, role: str | None = None, sent: "list | None" = None,
          max_wait_s: float | None = None,
          throttled: "list | None" = None,
          meter: "list | None" = None) -> dict:
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
    import time as _time
    _wait_t0 = _time.perf_counter()
    _rl.acquire(
        ep.provider,
        declared=declared,
        tokens_estimate=_estimate_tokens(payload),
        max_wait_s=_wait_cap,
    )
    # Time spent queued behind the rate limiter is part of the LLM number the
    # Perf page shows; record it on its own so a slow "LLM" row can be told
    # apart from a throttled one.
    _waited_ms = (_time.perf_counter() - _wait_t0) * 1000.0
    if _waited_ms >= 50:
        try:
            from aiforge_core.runtime import perf_recorder
            perf_recorder.record("Queue", role or ep.provider, _waited_ms)
        except Exception:  # noqa: BLE001
            pass
    cancel = _CANCEL.get()
    _owned_cancel = None
    if cancel is None and role:
        try:
            from aiforge_core.llm._rate_settings import _category
            if _category(role) == "compaction":
                _owned_cancel = threading.Event()
                from aiforge_core.llm.interactive_gate import track_background
                track_background(_owned_cancel)
                cancel = _owned_cancel
        except Exception:  # noqa: BLE001
            _owned_cancel = None
    try:
        return _post_after_cancel(ep, payload, timeout_s, role=role, sent=sent,
                                  throttled=throttled, meter=meter, cancel=cancel)
    finally:
        if _owned_cancel is not None:
            try:
                from aiforge_core.llm.interactive_gate import untrack_background
                untrack_background(_owned_cancel)
            except Exception:  # noqa: BLE001
                pass


def _post_after_cancel(ep, payload, timeout_s, *, role, sent, throttled, meter, cancel):
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
    # The one gateway. meter=False here: this path counts through
    # _record_request below, which has the cancel-check that must sit BETWEEN
    # the throttle and the count. Provider-scoped so a 429 from a cloud gateway
    # does not stall the local mlx server; role picks the category sub-ceiling.
    _throttled, _ = _rl.govern_send(
        role=role, provider=ep.provider,
        max_wait_s=float(_int_env("AIFORGE_LLM_MAX_WAIT_S", 120)),
        meter=False)
    if throttled is not None:
        throttled[0] = _throttled
    # ONE meter token for BOTH paths, and the failure counted here rather than
    # in _post_with_retry: this function is what counts an attempt, so this is
    # the only place where sends and failures cannot drift apart (the retry
    # wrapper sees a chain, and the callers above it — client.complete's
    # empty-response loop, the pipeline — start several chains per answer).
    # Already stopped? Then nothing is going out, and counting a request here
    # would have the meter invent traffic for a box that sent none — the same
    # phantom the preflight ordering above exists to prevent. _post_cancellable
    # raises this on its own first line; raising it here only skips the count.
    if cancel is not None and cancel.is_set():
        raise _LLMCancelled("cancelled before request")
    _tok = _record_request(role, ep.provider, ep.model)
    if meter is not None:
        # Hand the token UP. A 200-OK whose content is empty/think-only is a
        # failed request that raises nothing, so this function cannot see it —
        # only the caller reading the body can, and it needs this exact
        # request's token to charge the failure to the right minute and turn.
        meter[0] = _tok
    try:
        if cancel is not None:
            return _post_cancellable(ep, payload, timeout_s, cancel, sent)
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
    except Exception as exc:  # noqa: BLE001
        # A cancelled generation lands here too (_LLMCancelled is an
        # Exception): a request the user stopped is still a request that cost
        # the endpoint and produced no answer, and it carries its own
        # "cancelled" label so it is distinguishable from a broken endpoint.
        _record_failure(_tok, exc)
        raise


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

    from aiforge_core.llm import endpoint_breaker as _breaker
    try:
        u = _urlparse(base_url)
        host = u.hostname
        if not host:
            return
        port = u.port or (443 if u.scheme == "https" else 80)
    except Exception:  # noqa: BLE001 — malformed url → let the real call surface it
        return
    # An endpoint that has just failed to connect, repeatedly, is skipped
    # without a network wait — otherwise every call (and every retry of every
    # call) paid the full connect budget against a host that is gone.
    skipped = _breaker.is_open(base_url)
    if skipped:
        raise ConnectionError(f"LLM endpoint unreachable ({host}:{port}): {skipped}")
    try:
        _socket.create_connection((host, port), timeout=ct).close()
    except OSError as exc:
        # This probe is connect-ONLY, so every failure here is a connect
        # failure — including the bare TimeoutError a sleeping host produces,
        # which the generic classifier could not tell from a read timeout.
        _breaker.record_failure(base_url, str(exc))
        raise ConnectionError(
            f"LLM endpoint unreachable ({host}:{port}) within {ct:g}s "
            f"connect budget: {exc}") from exc
    _breaker.record_success(base_url)
