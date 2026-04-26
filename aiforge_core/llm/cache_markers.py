"""Provider-aware prompt-cache markers.

Each cloud LLM has a different cache-control protocol:
- **Anthropic**: ``cache_control: {"type": "ephemeral"}`` block on
  the last 2 user messages.
- **Gemini**: explicit cache via ``Cached Content API`` (token-cost
  + TTL); we set ``cache: True`` on system + first long user msg
  through litellm/openai-compat passthrough.
- **OpenAI**: ``prompt_cache_key`` parameter (responses API) or
  automatic prefix caching (chat-completions ≥ 1024-token prefix).
- **Ollama Cloud**: no explicit cache yet; pass through.

KISS: one ``stamp(messages, model, provider)`` entry. Caller wraps
its outgoing payload before POSTing. Returns *new* messages list
(does not mutate input).

Toggle off via ``AIFORGE_PROMPT_CACHE=0`` (default on).
"""
from __future__ import annotations

import os
from typing import Iterable


def is_enabled() -> bool:
    return os.environ.get("AIFORGE_PROMPT_CACHE", "1") == "1"


def stamp(
    messages: list[dict], *, model: str, provider: str,
) -> list[dict]:
    """Return a new messages list with provider-specific cache hints."""
    if not is_enabled() or not messages:
        return list(messages)

    prov = (provider or "").lower()
    if prov == "anthropic":
        return _stamp_anthropic(messages)
    if prov == "gemini":
        return _stamp_gemini(messages)
    if prov in ("openai", "ollama_cloud"):
        # Both serve OpenAI-compat /chat/completions; OpenAI's prefix
        # cache is automatic when the same system prompt repeats.
        # Nothing to inject — leave messages untouched.
        return list(messages)
    return list(messages)


def apply_to_session(session: object, *, provider: str) -> None:
    """Monkey-patch ``session.raw_ask`` to stamp cache markers on the
    outgoing messages list. KISS — wraps the existing generator so the
    rest of the GA pipeline is untouched.

    Idempotent: a second call detects our marker and skips re-wrap.
    """
    if not is_enabled():
        return
    if getattr(session, "_aiforge_cache_wrapped", False):
        return
    orig = session.raw_ask  # type: ignore[attr-defined]
    model = getattr(session, "model", "")

    def _wrapped(messages, *a, **kw):
        try:
            messages = stamp(list(messages), model=model, provider=provider)
        except Exception:
            pass
        return orig(messages, *a, **kw)

    session.raw_ask = _wrapped  # type: ignore[assignment]
    session._aiforge_cache_wrapped = True  # type: ignore[attr-defined]


def cache_key_for(model: str, role: str) -> str:
    """Stable key per (role, model) — used by OpenAI Responses API
    `prompt_cache_key` param. Two distinct roles never share cache."""
    return f"aiforge:{role}:{_short_model(model)}"


# ───────── helpers ─────────────────────────────────────────────────


def _stamp_anthropic(messages: list[dict]) -> list[dict]:
    """Anthropic ephemeral cache on last 2 user messages."""
    out = [dict(m) for m in messages]
    user_idxs = [i for i, m in enumerate(out) if m.get("role") == "user"]
    for idx in user_idxs[-2:]:
        msg = out[idx]
        c = msg.get("content")
        if isinstance(c, str):
            msg["content"] = [{
                "type": "text", "text": c,
                "cache_control": {"type": "ephemeral"},
            }]
        elif isinstance(c, list) and c:
            new_c = list(c)
            last = dict(new_c[-1])
            last["cache_control"] = {"type": "ephemeral"}
            new_c[-1] = last
            msg["content"] = new_c
        out[idx] = msg
    # System block too (most ROI for system prompt cache).
    if out and out[0].get("role") == "system":
        sys = dict(out[0])
        sc = sys.get("content")
        if isinstance(sc, str):
            sys["content"] = [{
                "type": "text", "text": sc,
                "cache_control": {"type": "persistent"},
            }]
        out[0] = sys
    return out


def _stamp_gemini(messages: list[dict]) -> list[dict]:
    """Gemini doesn't expose cache via the OpenAI-compat shim that
    AI Studio fronts. Best we can do via that path is a no-op; full
    cache would need switching to the native ``cachedContents`` API.
    Keep as stub so the surface stays uniform with anthropic.
    """
    return list(messages)


def _short_model(model: str) -> str:
    if "/" in model:
        return model.rsplit("/", 1)[-1]
    return model
