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


def _record_usage(role: str, resp_body: dict) -> None:
    """Push token counts to registry. Best-effort no-op (ga_tools removed)."""
    pass
