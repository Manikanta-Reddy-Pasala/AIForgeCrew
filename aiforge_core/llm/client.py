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

from . import providers as _providers
from . import rate_limiter as _rl
from ._ssl import _ca_bundle as _ssl_ca_bundle
from ._ssl import auto_relax_internal as _ssl_auto_relax
from ._ssl import context_for as _ssl_context_for
from ._ssl import insecure_context as _ssl_insecure
from .router import escalate, fallback, resolve
from .types import Endpoint

_log = logging.getLogger("aiforge.llm.client")

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


class _LLMCancelled(Exception):
    """Raised when a post is aborted because its cancel event fired — classified
    non-retryable so the retry loop doesn't re-issue the cancelled call."""

# Endpoint.extras keys that are transport/routing control — never sent as
# OpenAI chat-completion body params (strict servers 400 on unknown keys).
_NON_BODY_EXTRA_KEYS = frozenset({"insecure_tls"})


# HTTP status codes that warrant in-place retry (transient): 408 timeout,
# 429 rate-limit, 500/502/503/504 server-side. 401/403 included because the
# self-hosted proxy (nginx) returns intermittent "401 Authorization
# Required" even with a valid token — bounded retries (AIFORGE_LLM_RETRY_MAX)
# ride over the blip instead of failing the chat. Disable the auth retries
# with AIFORGE_LLM_RETRY_AUTH=0 if your endpoint's 401 is always real.
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
if os.environ.get("AIFORGE_LLM_RETRY_AUTH", "1") not in ("0", "false", "no"):
    _TRANSIENT_HTTP = _TRANSIENT_HTTP | {401, 403}


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


def _estimate_tokens(payload: bytes) -> int:
    """Rough token estimate from payload bytes — 4 chars ≈ 1 token.

    Good-enough budget for the limiter; the API's exact accounting
    happens server-side.
    """
    return max(1, len(payload) // 4)


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


def _int_env(name: str, default: int) -> int:
    import os as _os
    try:
        return int(_os.environ.get(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    import os as _os
    try:
        return float(_os.environ.get(name, default))
    except ValueError:
        return default


def _http_err_body(exc: Exception) -> str:
    """Best-effort read of an HTTPError response body (the proxy's actual
    rejection detail, e.g. which param it didn't like). urllib's HTTPError
    is a file-like; reading it is one-shot, so guard against re-reads."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    # Prefer a body already read by the classifier (exc.read() is one-shot).
    raw = getattr(exc, "_aiforge_body", None)
    if raw is None:
        try:
            raw = exc.read()
        except Exception:
            return ""
    try:
        return raw.decode("utf-8", "replace")[:600]
    except Exception:
        return str(raw)[:600]


# Model-lifecycle phrases a local OpenAI-compatible server emits when the
# model isn't resident (idle-unload / OOM-evict / restart / not-yet-loaded).
# Endpoint-agnostic — mlx-lm, ollama, llama.cpp, vLLM, LM Studio all surface a
# variant. A server may return these as a 200-OK error body OR a 4xx, so we
# match the message text either way and RETRY (gives the server time to
# reload) instead of hard-failing the run.
_MODEL_DROP_MARKERS = (
    "model unloaded", "unloaded", "model not loaded", "not loaded",
    "model not found", "no model", "no models loaded", "model is loading",
    "loading model", "model not ready", "still loading",
)


class _ModelReloading(Exception):
    """Raised when the endpoint reports the model is unloaded/reloading — a
    transient condition that should retry, not fail the run."""


def _raise_if_model_dropped(body: object) -> None:
    """If ``body`` is an OpenAI-style error whose message names a model drop,
    raise :class:`_ModelReloading` so the retry loop re-issues the call."""
    err = body.get("error") if isinstance(body, dict) else None
    if err is None:
        return
    msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
    low = msg.lower()
    if any(m in low for m in _MODEL_DROP_MARKERS):
        raise _ModelReloading(f"model unavailable (reloading?): {msg[:200]}")


def _is_transient_exc(exc: Exception) -> tuple[bool, str]:
    """Return (retry?, label) for transport exceptions.

    HTTPError 5xx / 408 / 429 → retry (server-side or rate-limit).
    HTTPError 4xx other → no retry, UNLESS its body names a model drop.
    _ModelReloading → retry. URLError / OSError / timeout → retry.
    """
    if isinstance(exc, _LLMCancelled):
        return False, "cancelled"        # don't re-issue an aborted call
    if isinstance(exc, _ModelReloading):
        return True, "model_reloading"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in _TRANSIENT_HTTP:
            return True, f"http_{exc.code}"
        # A 4xx whose body names a model drop is still transient (the server
        # is reloading), not a permanent bad-request. NOTE: exc.read() is
        # one-shot — stash the bytes on the exc so _http_err_body can log the
        # server's actual rejection reason (else the 400 cause is invisible).
        try:
            _body = exc.read()
            exc._aiforge_body = _body  # type: ignore[attr-defined]
            if _body and any(m in _body.decode("utf-8", "replace").lower()
                             for m in _MODEL_DROP_MARKERS):
                return True, "model_reloading_4xx"
        except Exception:  # noqa: BLE001
            pass
        return False, f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return True, "url_error"
    if isinstance(exc, TimeoutError):
        return True, "timeout"
    if isinstance(exc, OSError):
        return True, "os_error"
    return False, exc.__class__.__name__


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


# Reasoning models (qwen3-coder, deepseek-r1, …) sometimes emit their chain of
# thought inside the *content* field wrapped in <think>…</think> (or the
# lookalikes below) instead of the separate reasoning_content channel. When
# they do, the real answer is whatever sits AFTER the closing tag — often
# nothing, because the model spent its whole budget thinking. Strip the block
# so the caller never sees raw reasoning as the answer, and so a think-only
# reply collapses to "" and trips the garbage/retry path.
# Reasoning models put their chain of thought at the START, then the answer.
# We only strip a LEADING think block (after optional whitespace) — NOT blocks
# mid-content, so a legitimate answer that CONTAINS a <think>/<reasoning> literal
# (code emitting this codebase's own tag regex, an XML/prompt template) is left
# intact instead of being silently corrupted.
_THINK_LEAD_RE = re.compile(
    r"^\s*<(think|thought|reasoning|thinking)\b[^>]*>.*?</\1>\s*",
    re.IGNORECASE | re.DOTALL,
)
# Closing-tag-ONLY leading reasoning: many chat templates (qwen3, deepseek-r1)
# inject the OPENING <think> into the prompt, so the model's content starts with
# raw reasoning and the FIRST tag it emits is the closer — "reasoning…</think>the
# answer". Strip from the start up to that first closer, but ONLY when NO opener
# appears before it (else _THINK_LEAD_RE already handled the paired block, and a
# code/XML literal like re.compile(r"<think>.*?</think>") keeps its opener so it
# is preserved) and NOT when the answer opens with a code fence.
_THINK_CLOSE_ONLY_RE = re.compile(
    r"^\s*(?!```)"
    r"(?:(?!<(?:think|thought|reasoning|thinking)\b).)*?"
    r"</(?:think|thought|reasoning|thinking)>\s*",
    re.IGNORECASE | re.DOTALL,
)
# A LEADING unclosed opener: the stream ran out mid-thought — everything from the
# opener to end-of-string is reasoning with no answer following → drop it.
_THINK_OPEN_RE = re.compile(
    r"^\s*<(think|thought|reasoning|thinking)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_think(text: str) -> str:
    if "<" not in text:
        return text
    # Strip one-or-more leading closed think blocks (reasoning-then-answer).
    prev = None
    while prev != text:
        prev = text
        text = _THINK_LEAD_RE.sub("", text, count=1)
    # Then a leading closing-tag-only reasoning block (opener consumed by the
    # chat template).
    text = _THINK_CLOSE_ONLY_RE.sub("", text, count=1)
    # …then a leading unclosed opener (pure reasoning, no answer).
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def _extract_text(resp_body: dict) -> str:
    msg = (resp_body.get("choices") or [{}])[0].get("message", {}) or {}
    content = _strip_think((msg.get("content") or "").strip())
    if content:
        return content
    # content was empty or pure <think> — fall back to the reasoning channel,
    # but strip any nested think markers there too (some proxies double-wrap).
    return _strip_think((msg.get("reasoning_content") or "").strip())


def _record_usage(role: str, resp_body: dict) -> None:
    """Push token counts to registry. Best-effort no-op (ga_tools removed)."""
    pass


def _is_garbage(text: str) -> bool:
    """Heuristic for a 200-OK but useless response.

    Triggers fallback when:
      - empty after trim
      - just an mlx-lm tool-call dump fragment ("<tool_call>" with no body)
      - well-known stop-token leak ("<|im_end|>" alone)
    """
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) < 3:
        return True
    if t in ("<tool_call>", "</tool_call>", "<|im_end|>", "<|endoftext|>"):
        return True
    return False


def _append_no_think(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` nudging the model to answer WITHOUT its
    reasoning phase — used only on an empty-response retry. Appends ' /no_think'
    to the last user turn (the Qwen3 / DeepSeek-R1 convention); harmless text for
    models that ignore it."""
    out = [dict(m) for m in (messages or [])]
    for m in reversed(out):
        if m.get("role") == "user":
            m["content"] = (str(m.get("content") or "").rstrip() + " /no_think")
            return out
    out.append({"role": "user", "content": "/no_think"})
    return out


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
    empty_retries = max(0, _int_env("AIFORGE_LLM_EMPTY_RETRIES", 2))
    for attempt in range(empty_retries + 1):
        if attempt == 0:
            payload = _build_body(ep, messages, temperature, max_tokens,
                                  top_p, extras)
        else:
            # Last post came back EMPTY. A reasoning model (qwen*-reasoning,
            # deepseek-r1) systematically spends its whole budget THINKING and
            # emits empty content — re-posting the identical body just repeats
            # that. Coax a DIRECT answer: append '/no_think' (Qwen/DeepSeek honor
            # it → skip the reasoning phase) and widen max_tokens so a still-
            # thinking model has room left to emit the answer.
            _mt = min(int((max_tokens or 4096) * 2), 32768)
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
        if not _is_garbage(text):
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
