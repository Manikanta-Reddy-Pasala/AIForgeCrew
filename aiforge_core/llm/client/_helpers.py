"""Leaf helpers for the LLM client: shared logger, env parsing, token
estimate, and the (best-effort no-op) usage recorder. No intra-package deps."""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.llm.client")


def _estimate_tokens(payload: bytes) -> int:
    """Rough token estimate from payload bytes — 4 chars ≈ 1 token.

    Good-enough budget for the limiter; the API's exact accounting
    happens server-side.
    """
    return max(1, len(payload) // 4)


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


def _record_usage(role: str, resp_body: dict, token=None) -> None:
    """Record what the provider says this response cost, in tokens.

    This was a `pass` — every response's ``usage`` block was read off the wire
    and thrown away, which is why "are we generating too much?" could only ever
    be answered by guessing. Requests do not answer it: forty one-line ReAct
    steps and one six-thousand-token essay are both "41 requests".

    Provider-reported, never estimated: an estimate cannot tell you whether
    asking the model for shorter answers worked. Never raises — accounting must
    not break a call that already succeeded.
    """
    try:
        usage = resp_body.get("usage") if isinstance(resp_body, dict) else None
        if not isinstance(usage, dict):
            return
        from aiforge_core.llm import call_meter as _meter
        _meter.record_tokens(
            role,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            token=token,
        )
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass
