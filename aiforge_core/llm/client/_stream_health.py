"""Health bounds for a STREAMED completion: a first-token deadline and an
inter-chunk idle deadline, instead of one 900s wall on every read.

A streamed call that is working sends bytes: the status line once the server
starts, then a chunk per token (or an SSE ``: keep-alive`` comment while it
thinks). A call that has gone quiet for far longer than the model ever needs
is stuck — a wedged server, a dropped upstream behind a proxy, a GPU hang —
and waiting out the full read timeout on it cost the user fifteen minutes per
attempt. Both bounds are applied as the SOCKET timeout, which is per ``recv``:
ANY byte (a token, a keep-alive comment, a chunk header) is liveness and
restarts the clock, so a slow but alive server is never cut off.

  AIFORGE_LLM_FIRST_TOKEN_S  — longest silence before the first streamed data
                               event (default 180). Scaled UP with the prompt:
                               prompt processing of a big prompt on a local
                               27B is minutes, so the bound is
                               max(this, prompt_tokens / AIFORGE_LLM_PREFILL_TOK_S).
  AIFORGE_LLM_PREFILL_TOK_S  — assumed worst-case prefill speed (default 200).
  AIFORGE_LLM_STREAM_IDLE_S  — longest silence between chunks once the answer
                               is streaming (default 120).

0 disables a bound (the caller's read timeout alone applies). On a first send
neither bound is LONGER than the caller's own timeout; once the same request
has stalled, the doubled bounds may exceed it (see ``StreamWatch._tightest``).

Adaptive (llm/request_health): each stall of the same request doubles both
bounds for its next send, and the prefill speed seen on an endpoint, when
slower than AIFORGE_LLM_PREFILL_TOK_S, is what the first-token bound assumes.
"""
from __future__ import annotations

import socket
import time

from ._helpers import _estimate_tokens, _float_env


class LLMStreamStalled(ConnectionError):
    """The model server went silent mid-call — no first token, or no chunk for
    the idle bound. Distinct from a read TIMEOUT on purpose: that one means
    "the server has the prompt and is working, do not re-POST", while a stalled
    stream is a broken call (the connection is closed, which aborts the
    generation on every mainstream server), so it is retried like a dropped
    connection. A ConnectionError, so every classifier that already treats a
    connection failure as an LLM issue — the retry wrapper, the escalation
    policy's markers, the native-tools fallback — handles it unchanged."""

    def __init__(self, phase: str, seconds: float) -> None:
        self.phase = phase
        self.seconds = seconds
        what = ("no first token" if phase == "first_token"
                else "the machine slept under it" if phase == "after_sleep"
                else "stream went idle")
        super().__init__(f"LLM stream stalled: {what} for {seconds:.0f}s "
                         "(read timed out waiting on the model server)")


def _health():
    from aiforge_core.llm import request_health
    return request_health


def first_token_s(payload: bytes | None, url: str = "") -> float:
    base = _float_env("AIFORGE_LLM_FIRST_TOKEN_S", 180.0)
    if base <= 0:
        return 0.0
    tps = _float_env("AIFORGE_LLM_PREFILL_TOK_S", 200.0)
    learned = _health().prefill_tps(url) if url else None
    if learned and (tps <= 0 or learned < tps):
        tps = learned
    scaled = (_estimate_tokens(payload or b"") / tps) if tps > 0 else 0.0
    return max(base, scaled) * _health().stall_scale()


def idle_s() -> float:
    idle = max(0.0, _float_env("AIFORGE_LLM_STREAM_IDLE_S", 120.0))
    return idle * _health().stall_scale()


class StreamWatch:
    """Moves one streamed call's socket between its two health bounds.

    ``sock`` may be None (a test double, or a connection that was never
    opened): the watch then does nothing and the reads behave as before."""

    def __init__(self, sock, payload: bytes | None, read_timeout: float | None,
                 url: str = ""):
        self.sock = sock
        self.read_timeout = float(read_timeout) if read_timeout else 0.0
        self.url = url
        self._tokens = _estimate_tokens(payload or b"")
        self._first = first_token_s(payload, url)
        self._idle = idle_s()
        self.phase = "first_token"
        self.bound = 0.0
        self._armed = time.monotonic()
        self._last_data = self._armed

    def _tightest(self, health: float) -> float:
        """The health bound when it is tighter than the caller's own read
        timeout; 0 when the caller's timeout governs. Once the same request
        has stalled (its bounds doubled) the health bound governs even past
        the caller's generic read timeout: the doubling exists so a slow
        prefill on a busy box eventually completes, and handing over to a
        fixed read timeout would end it as a plain timeout instead. The
        stream-health detector stays the guard (a stall is still a stall)."""
        if health <= 0:
            return 0.0
        if self.read_timeout and health >= self.read_timeout \
                and _health().stall_scale() <= 1.0:
            return 0.0
        return health

    def _apply(self, health: float) -> None:
        self.bound = self._tightest(health)
        budget = _health().stall_budget_s()
        if self.bound and budget > 0:
            # Never past the request's stall budget: the last send waits only
            # what is left of it.
            self.bound = min(self.bound, max(1.0, budget - _health().stalled_seconds()))
        if self.sock is None or not self.bound:
            return
        try:
            self.sock.settimeout(self.bound)
        except (OSError, AttributeError):
            self.bound = 0.0

    def arm_first_token(self) -> None:
        self.phase = "first_token"
        self._armed = self._last_data = time.monotonic()
        self._apply(self._first)

    def check_silence(self) -> None:
        """A non-data line arrived (an SSE ``: keep-alive`` comment, a blank
        line). Keep-alives are liveness — a slow prefill sends them, and is
        given ``AIFORGE_LLM_KEEPALIVE_FACTOR`` (default 4) times the bound —
        but not forever: a proxy that keeps the stream alive for an upstream
        that is gone held the call with no end. Measured from the last DATA
        event."""
        if not self.bound:
            return
        from ._helpers import _float_env
        factor = max(1.0, _float_env("AIFORGE_LLM_KEEPALIVE_FACTOR", 4.0))
        limit = self.bound * factor
        budget = _health().stall_budget_s()
        if budget > 0:
            limit = min(limit, max(self.bound, budget - _health().stalled_seconds()))
        quiet = time.monotonic() - self._last_data
        if quiet > limit:
            _health().note_stall(quiet)
            raise _over_budget(LLMStreamStalled(self.phase, quiet), self.url)

    def got_data(self) -> None:
        """First real data event: from now on the idle bound applies. The
        wait for it is this endpoint's prefill speed, learned."""
        self._last_data = time.monotonic()
        if self.phase != "idle":
            self.phase = "idle"
            _health().note_prefill(self.url, self._tokens,
                                   time.monotonic() - self._armed)
            self._apply(self._idle)

    def release(self) -> None:
        """Back to the caller's read timeout — for a server that ignored
        ``stream`` and will send the whole body at once, after the full
        generation: silence there is normal, not a stall."""
        self.bound = 0.0
        if self.sock is None:
            return
        try:
            self.sock.settimeout(self.read_timeout or None)
        except (OSError, AttributeError):
            pass

    def stalled(self, exc: BaseException) -> BaseException:
        """``exc`` as an LLMStreamStalled when one of OUR bounds fired it; else
        ``exc`` unchanged (the caller's own read timeout keeps its meaning)."""
        if self.bound and isinstance(exc, (socket.timeout, TimeoutError)):
            _health().note_stall(self.bound)   # the next send waits twice as long
            return _over_budget(LLMStreamStalled(self.phase, self.bound), self.url)
        return exc


def _over_budget(stall: "LLMStreamStalled", url: str) -> BaseException:
    """``stall`` — or, once this request has waited out its whole stall budget
    across its sends, the LLM issue that ends it (not retried, not waited
    for): more of the same silence will not answer it."""
    budget = _health().stall_budget_s()
    spent = _health().stalled_seconds()
    if budget <= 0 or spent < budget:
        return stall
    from aiforge_core.llm.model_outage import LLMRequestFailing
    mins = spent / 60.0
    return LLMRequestFailing(url, _health().current().stalls if _health().current() else 1,
                             stall, (
        f"LLM issue: the model at {url or '?'} sent nothing back for "
        f"{mins:.0f} min in total across {_sends()} sends of this request "
        f"(last: {stall}). The connection to it may be dropping long "
        "requests (a Docker/WSL network that closes idle connections, a "
        "proxy timeout), or the prompt is too big for it to start in time. "
        "Raise AIFORGE_LLM_STALL_BUDGET_S to wait longer."))


def _sends() -> int:
    h = _health().current()
    return (h.stalls if h is not None else 0) or 1


__all__ = ["LLMStreamStalled", "StreamWatch", "first_token_s", "idle_s"]
