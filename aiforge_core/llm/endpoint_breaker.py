"""Remember which model endpoints cannot be connected to, for a short while.

Every LLM call probes its endpoint with a TCP connect (8 s budget) and retries
up to three times, and nothing carried that knowledge to the NEXT call. So when
a host went away — the Mac Studio moved from .185 to .167 — every chat turn,
every learner fold and every enhancer call paid up to 3 x 8 s against the dead
host before falling back, over and over: turns that made no tool calls at all
took 20 to 40 minutes.

This is a circuit breaker keyed by host:port and shared by the chat client and
the ADK pipeline:

* it OPENS after ``AIFORGE_LLM_BREAKER_FAILS`` (default 2) consecutive
  CONNECT failures — a single dropped SYN does not take an endpoint out;
* while open, a call to that endpoint fails at once, without touching the
  network, so the caller moves straight to its next endpoint — and when every
  endpoint is down, the caller WAITS (llm/model_wait) instead of failing: its
  probe is the half-open check, and a probe that answers closes the breaker;
* after ``AIFORGE_LLM_BREAKER_COOLDOWN_S`` (default 30) the next call probes
  again (half-open), and one successful connect closes it.

Only connect-level failures count. A read timeout, a 5xx or a bad answer means
the server IS there, and skipping it would hide a real, reachable model.
``AIFORGE_LLM_BREAKER_COOLDOWN_S=0`` disables the breaker.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

_DEFAULT_FAILS = 2
_DEFAULT_COOLDOWN_S = 30.0


@dataclass
class _State:
    fails: int = 0
    open_until: float = 0.0
    reason: str = ""


_LOCK = threading.Lock()
_STATE: dict[str, _State] = {}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _cooldown_s() -> float:
    return max(0.0, _env_float("AIFORGE_LLM_BREAKER_COOLDOWN_S", _DEFAULT_COOLDOWN_S))


def _fails_to_open() -> int:
    return max(1, int(_env_float("AIFORGE_LLM_BREAKER_FAILS", _DEFAULT_FAILS)))


def endpoint_key(url: str | None) -> str | None:
    """``host:port`` for a base URL, or None when it cannot be parsed."""
    try:
        u = urlparse(url or "")
        if not u.hostname:
            return None
        port = u.port or (443 if u.scheme == "https" else 80)
        return f"{u.hostname}:{port}"
    except (ValueError, TypeError):
        return None


def is_open(url: str | None) -> str | None:
    """The reason this endpoint is being skipped, or None to go ahead.

    Past the cooldown the breaker is half-open: this returns None so exactly
    the next call probes the endpoint again.
    """
    if _cooldown_s() <= 0:
        return None
    key = endpoint_key(url)
    if key is None:
        return None
    with _LOCK:
        st = _STATE.get(key)
        if st is None or st.open_until <= 0:
            return None
        left = st.open_until - time.monotonic()
        if left <= 0:
            return None
        return (f"{key} failed to connect {st.fails} time(s) in a row "
                f"({st.reason}); skipping it for another {left:.0f}s")


def record_failure(url: str | None, reason: str = "") -> None:
    """Count one CONNECT failure; open the breaker once the threshold is hit."""
    cooldown = _cooldown_s()
    key = endpoint_key(url)
    if cooldown <= 0 or key is None:
        return
    with _LOCK:
        st = _STATE.setdefault(key, _State())
        st.fails += 1
        st.reason = (reason or "connect failed")[:160]
        if st.fails >= _fails_to_open():
            st.open_until = time.monotonic() + cooldown


def record_success(url: str | None) -> None:
    """A connect worked: the endpoint is back, forget its failures."""
    key = endpoint_key(url)
    if key is None:
        return
    with _LOCK:
        _STATE.pop(key, None)


def reset() -> None:
    """Forget everything (tests, and an operator changing the endpoint)."""
    with _LOCK:
        _STATE.clear()


#: errno values that mean "could not reach the host at all".
_CONNECT_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
    errno.EHOSTDOWN, errno.ETIMEDOUT, errno.ECONNABORTED,
})
#: Phrases the HTTP stacks use for the same thing when the errno is buried —
#: litellm, in particular, re-raises with the socket error only in its text.
_CONNECT_PHRASES = (
    "connection refused", "no route to host", "network is unreachable",
    "host is down", "name or service not known", "nodename nor servname",
    "temporary failure in name resolution", "failed to establish a new connection",
    "connection timed out", "connect timeout", "connecttimeout",
    "endpoint unreachable",
)
#: ...and phrases that mean the connection WAS made. These win.
_REACHED_PHRASES = ("read timed out", "readtimeout", "read timeout",
                    "remote end closed", "incomplete read")
#: Exception classes (httpx) that are unambiguously a connect failure. Note
#: litellm.APIConnectionError is NOT here: it also wraps failures that happen
#: after the connection is up, so it only counts when its text or cause says so.
_CONNECT_CLASSES = ("connecterror", "connecttimeout")


def _one_is_connect(exc: BaseException) -> bool | None:
    """True / False when this link decides it, None to keep walking."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError) and exc.errno in _CONNECT_ERRNOS:
        return True
    text = str(exc).lower()
    if any(p in text for p in _REACHED_PHRASES):
        return False
    if type(exc).__name__.lower() in _CONNECT_CLASSES:
        return True
    if any(p in text for p in _CONNECT_PHRASES):
        return True
    return None


def is_connect_error(exc: BaseException | None) -> bool:
    """True only for a failure to REACH the endpoint.

    A read timeout, an HTTP error or a malformed answer all mean the server
    answered — or at least accepted the connection — and must not trip the
    breaker. Walks the cause chain, because every stack in play wraps the
    socket error at least once.

    Callers that KNOW they were only connecting (the TCP preflight) should
    record the failure directly rather than ask: a connect timeout from
    ``socket.create_connection`` is a bare ``TimeoutError('timed out')``, which
    this cannot tell apart from a read timeout.
    """
    seen = 0
    while exc is not None and seen < 8:
        verdict = _one_is_connect(exc)
        if verdict is not None:
            return verdict
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


__all__ = ["endpoint_key", "is_connect_error", "is_open", "record_failure",
           "record_success", "reset"]
