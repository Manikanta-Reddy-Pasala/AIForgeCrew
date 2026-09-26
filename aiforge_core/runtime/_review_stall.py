"""The PR reviewer's read timeout, judged like the client's stream stall
(split from :mod:`aiforge_core.runtime.pr_reviewer`)."""
from __future__ import annotations

from typing import Any


def _is_read_timeout(exc: BaseException) -> bool:
    """litellm's / httpx's READ timeout: the per-read bound ran out. A
    CONNECT timeout is not one — the server never had the request (an
    outage, never a stall of this request)."""
    links = [e for e in (exc, exc.__cause__, exc.__context__) if e]
    names = {type(e).__name__.lower() for e in links}
    if names & {"connecttimeout", "connecterror", "connectionerror",
                "apiconnectionerror"}:
        return False
    if any("connect" in str(e).lower() for e in links):
        return False
    try:
        from aiforge_core.llm import endpoint_breaker
        if endpoint_breaker.is_connect_error(exc):
            return False
    except Exception:  # noqa: BLE001
        pass
    return bool(names & {"timeout", "apitimeouterror", "readtimeout",
                         "timeoutexception", "timeouterror"})


def as_stall(exc: BaseException, ep: dict[str, Any], messages: list):
    """A read that timed out at :func:`_read_bound` is a STALL of this
    request, exactly like the client's stream watch cutting one: counted in
    llm/request_health (so the next send's bound doubles) and raised as
    ``LLMStreamStalled``, which model_wait judges — while the server is still
    busy with the abandoned request the liveness probe queues, so the resend
    waits for the probe to be answered instead of piling another prompt on."""
    if not _is_read_timeout(exc):
        return None
    try:
        from aiforge_core.llm import request_health
        from aiforge_core.llm.client._stream_health import LLMStreamStalled
        from aiforge_core.runtime.pr_reviewer import _read_bound
        bound = _read_bound(ep, messages) or 0.0
        request_health.note_stall()
        return LLMStreamStalled("first_token", float(bound))
    except Exception:  # noqa: BLE001
        return None


__all__ = ["as_stall"]
