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

import json
import logging
import os
import random
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

# Endpoint.extras keys that are transport/routing control — never sent as
# OpenAI chat-completion body params (strict servers 400 on unknown keys).
_NON_BODY_EXTRA_KEYS = frozenset({"insecure_tls", "claude_bin", "claude_host"})


# HTTP status codes that warrant in-place retry (transient): 408 timeout,
# 429 rate-limit, 500/502/503/504 server-side. 4xx other than 408/429 are
# permanent — fall over to next provider immediately.
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def _build_body(ep: Endpoint, messages: list[dict],
                temperature: float | None,
                max_tokens: int | None,
                top_p: float | None,
                extras: dict | None) -> bytes:
    body: dict = {
        "model": ep.model,
        "messages": messages,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if top_p is not None:
        body["top_p"] = top_p
    # Provider-bundled extras first, then per-call extras override. Strip
    # transport-control keys (TLS opt-out, claude CLI routing) — they live
    # on the Endpoint for _post / the CLI path, NOT as chat-completion body
    # params. Leaking insecure_tls into the body makes strict servers (e.g.
    # Open WebUI) reject the request with HTTP 400.
    body.update({k: v for k, v in ep.extras.items()
                 if k not in _NON_BODY_EXTRA_KEYS})
    if extras:
        body.update(extras)
    # LM Studio rejects response_format.type=json_object — only json_schema or text.
    if ep.provider == "local":
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
    # Claude subscription path — bypass HTTP and shell out to `claude` CLI.
    # Subscription auth lives in the OS keychain, not as an API key.
    if ep.provider == "claude_local":
        return _send_via_claude_cli(ep, payload, timeout_s)
    req = urllib.request.Request(
        f"{ep.base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ep.api_key}",
            # Some proxies/WAFs reject the stdlib Python-urllib UA.
            "User-Agent": os.environ.get(
                "AIFORGE_LLM_USER_AGENT", "curl/8.5.0 (aiforge)"),
        },
        method="POST",
    )
    # Per-endpoint TLS context. Skip verification when the role carries the
    # explicit insecure_tls opt-out OR the host is trusted-internal (self-
    # hosted LAN box, self-signed is normal). Public hosts verify; a CA
    # bundle keeps verify ON. Otherwise honour AIFORGE_LLM_SSL_VERIFY / CA.
    base = ep.base_url
    insecure = bool((ep.extras or {}).get("insecure_tls"))
    if str(base).lower().startswith("https://") and (
        insecure or _ssl_auto_relax(base)
    ) and not _ssl_ca_bundle():
        ctx = _ssl_insecure()
    else:
        ctx = _ssl_context_for(base)
    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
        return json.loads(resp.read())


def _send_via_claude_cli(ep: Endpoint, payload: bytes, timeout_s: int) -> dict:
    """Run `claude --print` and shape the result into an OpenAI-compat
    chat-completion response. Honours AIFORGE_CLAUDE_HOST for SSH routing
    (e.g. NUC → Mac Studio where the subscription keychain lives)."""
    import subprocess

    body = json.loads(payload)
    messages = body.get("messages") or []
    # Flatten messages into a single prompt — claude CLI takes one stdin.
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(f"<|{role}|>\n{content}")
    prompt = "\n\n".join(parts)

    bin_name = ep.extras.get("claude_bin", "claude") if ep.extras else "claude"
    host = ep.extras.get("claude_host", "") if ep.extras else ""
    cmd = [bin_name, "--print"]
    if ep.model:
        cmd += ["--model", ep.model]
    if host:
        cmd = ["ssh", host, " ".join(cmd)]

    proc = subprocess.run(
        cmd, input=prompt.encode(), capture_output=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude_local subprocess failed: rc={proc.returncode} "
            f"stderr={proc.stderr.decode(errors='replace')[:500]}"
        )
    text = proc.stdout.decode(errors="replace").strip()
    return {
        "id": "claude-local-stub",
        "model": ep.model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": _estimate_tokens(payload),
                  "completion_tokens": max(1, len(text) // 4),
                  "total_tokens": _estimate_tokens(payload) + max(1, len(text) // 4)},
    }


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


def _is_transient_exc(exc: Exception) -> tuple[bool, str]:
    """Return (retry?, label) for transport exceptions.

    HTTPError 5xx / 408 / 429 → retry (server-side or rate-limit).
    HTTPError 4xx other → no retry (permanent: bad auth, bad model id).
    URLError / OSError / timeout / connection-reset → retry (transport flake).
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in _TRANSIENT_HTTP:
            return True, f"http_{exc.code}"
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
                _log.warning(
                    "llm.transport_error role=%s provider=%s model=%s "
                    "url=%s/chat/completions label=%s attempt=%d err=%s",
                    role, ep.provider, ep.model,
                    str(ep.base_url).rstrip("/"), label, attempt,
                    str(exc)[:300],
                    extra={"aiforge": {"role": role, "provider": ep.provider,
                                       "model": ep.model, "source": source,
                                       "attempt": attempt, "label": label,
                                       "fatal": not retry,
                                       "error": str(exc)[:200]}},
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


def _extract_text(resp_body: dict) -> str:
    msg = (resp_body.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if content:
        return content
    return (msg.get("reasoning_content") or "").strip()


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


def _try_post(ep: Endpoint, messages: list[dict],
              *, temperature, max_tokens, top_p, extras,
              timeout_s: int, role: str,
              source: str) -> tuple[str, dict] | None:
    """Single attempt against ``ep``. Returns (text, raw_body) on success
    (text passing :func:`_is_garbage`), or ``None`` on transport error or
    garbage. Caller decides whether to escalate / fall back."""
    payload = _build_body(ep, messages, temperature, max_tokens, top_p, extras)
    try:
        body = _post_with_retry(ep, payload, timeout_s,
                                role=role, source=source)
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, TimeoutError):
        # _post_with_retry already logged the final attempt — caller
        # falls over to fallback() / escalate() per the retry chain.
        return None
    _record_usage(role, body)
    text = _extract_text(body)
    if _is_garbage(text):
        _log.warning(
            "llm.empty_response",
            extra={"aiforge": {"role": role, "provider": ep.provider,
                               "model": ep.model, "source": source,
                               "preview": text[:80]}},
        )
        return None
    return text, body


def complete(role: str, messages: list[dict], *,
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
    # long chain-of-thought) routinely need minutes — a short timeout shows
    # up as intermittent "timeout" transport errors. Generous default,
    # tunable via AIFORGE_LLM_TIMEOUT_S.
    if timeout_s is None:
        timeout_s = _int_env("AIFORGE_LLM_TIMEOUT_S", 600)

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
