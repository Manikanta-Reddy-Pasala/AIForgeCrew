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
import urllib.error
import urllib.request

from .router import resolve, fallback, escalate
from .types import Endpoint
from . import providers as _providers
from . import rate_limiter as _rl


_log = logging.getLogger("aiforge.llm.client")


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
    # Provider-bundled extras first, then per-call extras override.
    body.update(ep.extras)
    if extras:
        body.update(extras)
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
    req = urllib.request.Request(
        f"{ep.base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ep.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def _int_env(name: str, default: int) -> int:
    import os as _os
    try:
        return int(_os.environ.get(name, default))
    except ValueError:
        return default


def _extract_text(resp_body: dict) -> str:
    msg = (resp_body.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if content:
        return content
    return (msg.get("reasoning_content") or "").strip()


def _record_usage(role: str, resp_body: dict) -> None:
    """Push token counts into ga_tools.tokens registry. Best-effort."""
    try:
        from aiforge_core.doer.ga_tools import tokens as _tk
        import os as _os
        usage = (resp_body or {}).get("usage") or {}
        ticket = _os.environ.get("AIFORGE_CURRENT_TICKET", "")
        _tk.note(
            ticket or None,
            role,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
        )
    except Exception:
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
        body = _post(ep, payload, timeout_s)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        _log.warning(
            "llm.transport_error",
            extra={"aiforge": {"role": role, "provider": ep.provider,
                               "model": ep.model, "source": source,
                               "error": str(exc)[:200]}},
        )
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
             timeout_s: int = 180) -> str:
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
        f"llm.exhausted role={role} primary={primary.provider} "
        f"fallback={fb.provider if fb else 'none'} "
        f"cloud={cloud.provider if cloud else 'none'} "
        f"— all providers returned transport error or empty content"
    )
