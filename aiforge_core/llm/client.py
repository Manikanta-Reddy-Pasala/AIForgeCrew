"""One-shot chat-completions client with provider fallback.

KISS surface — just call :func:`complete`. Builds an OpenAI-compat
request body, posts to the resolved endpoint, returns
``message.content`` (or ``reasoning_content`` when content empty).
Retries once against the fallback endpoint on transport error.

Per-call kwargs map to OpenAI body fields:
``temperature``, ``max_tokens``, ``top_p``, ``timeout_s``,
``extras`` (merged into body verbatim — pass
``{"chat_template_kwargs": {...}}`` for mlx-lm template kwargs).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .router import resolve, fallback
from .types import Endpoint
from . import providers as _providers
from . import rate_limiter as _rl


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


def complete(role: str, messages: list[dict], *,
             temperature: float | None = None,
             max_tokens: int | None = None,
             top_p: float | None = None,
             extras: dict | None = None,
             timeout_s: int = 180) -> str:
    """Issue one chat-completion call for ``role``.

    Returns the assistant text. Falls back to the other provider
    on the FIRST transport error; subsequent failures raise.
    """
    primary: Endpoint = resolve(role)
    payload = _build_body(
        primary, messages, temperature, max_tokens, top_p, extras,
    )
    try:
        return _extract_text(_post(primary, payload, timeout_s))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        fb = fallback(role)
        if fb is None:
            raise
        # Same body but with the fallback's model swapped in.
        fb_payload = _build_body(
            fb, messages, temperature, max_tokens, top_p, extras,
        )
        try:
            return _extract_text(_post(fb, fb_payload, timeout_s))
        except Exception:
            raise exc  # surface the primary error, fallback also broken
