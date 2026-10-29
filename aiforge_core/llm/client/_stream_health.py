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

import errno
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

    def __init__(self, phase: str, seconds: float, keepalive: bool = False) -> None:
        self.phase = phase
        self.seconds = seconds
        self.keepalive = keepalive
        #: Seconds of the turn's stall budget left after this stall (None = no
        #: budget): set by the stream watch, read by the re-send status line.
        self.budget_left = None
        what = ("only keep-alives, no data" if keepalive
                else "no first token" if phase == "first_token"
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

    def _budget(self) -> float:
        """The turn's stall budget as it applies to this request (0 = none)."""
        budget = _health().stall_budget_s()
        if budget <= 0:
            return 0.0
        import os
        plain = self._first / max(1.0, _health().stall_scale())
        budget = max(budget, 3.0 * plain)
        if (os.environ.get("AIFORGE_LLM_TIMEOUT_S") or "").strip():
            budget = max(budget, self.read_timeout)   # the user's own choice
        return budget

    def _budget_left(self) -> float:
        """Seconds of the turn's stall budget still to spend; 0 = no budget.
        Never shorter than one doubled send of this request (3x its plain
        first-token bound), nor than ``AIFORGE_LLM_TIMEOUT_S`` when the user
        set it: a slow prefill the doubling exists for must still fit, and a
        user who raised the timeout is not cut short by a default."""
        budget = self._budget()
        if not budget:
            return 0.0
        # Never below this request's plain first-token bound: every send
        # gets a fair wait; the budget decides only whether there is another.
        plain = self._first / max(1.0, _health().stall_scale())
        return max(1.0, plain, budget - _health().stalled_seconds())

    def _apply(self, health: float) -> None:
        self.bound = self._tightest(health)
        left = self._budget_left()
        if self.bound and left and self.phase == "first_token":
            # The last send waits only what is left of the budget. Not once
            # data flows: a model visibly answering is alive.
            self.bound = min(self.bound, left)
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
        given ``AIFORGE_LLM_KEEPALIVE_FACTOR`` (default 4) times the bound (the
        caller's read timeout when no bound applies) — but not forever: a
        proxy that keeps the stream alive for an upstream that is gone held
        the call with no end. Measured from the last DATA event."""
        base = self.bound or self.read_timeout
        if not base:
            return
        from ._helpers import _float_env
        factor = max(1.0, _float_env("AIFORGE_LLM_KEEPALIVE_FACTOR", 4.0))
        limit = base * factor
        left = self._budget_left()
        if left and self.phase == "first_token":
            limit = min(limit, max(base, left))
        quiet = time.monotonic() - self._last_data
        if quiet > limit:
            _health().note_stall(quiet)
            raise _over_budget(LLMStreamStalled(self.phase, quiet, keepalive=True),
                               self.url, self._budget())

    def got_data(self) -> None:
        """First real data event: from now on the idle bound applies. The
        wait for it is this endpoint's prefill speed, learned."""
        self._last_data = time.monotonic()
        if self.phase != "idle":
            self.phase = "idle"
            _health().note_turn_answer()  # silence in a row starts again
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
        ``exc`` unchanged (the caller's own read timeout keeps its meaning).
        A read that failed with ETIMEDOUT is TCP keepalive finding the peer
        gone: a dropped connection (re-sent), not a timeout of ours or a
        shipped one."""
        if getattr(exc, "errno", None) == errno.ETIMEDOUT:
            quiet = time.monotonic() - self._last_data
            _health().note_stall(quiet)        # dead time counts like silence
            return _over_budget(LLMConnectionDead(quiet), self.url, self._budget())
        if self.bound and isinstance(exc, (socket.timeout, TimeoutError)):
            quiet = time.monotonic() - self._last_data
            _health().note_stall(quiet)        # the next send waits twice as long
            return _over_budget(LLMStreamStalled(self.phase, quiet), self.url,
                                self._budget())
        return exc


class LLMConnectionDead(ConnectionResetError):
    """TCP keepalive found the model connection dead (a NAT or proxy dropped
    it). Re-sent like any dropped connection; never evidence that the model
    server crashed on the request."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.phase = "dead"
        self.keepalive = False
        self.budget_left = None
        super().__init__(errno.ECONNRESET,
                         "the connection to the model server died (no answer "
                         f"to TCP keepalive after {seconds:.0f}s)")


def _over_budget(stall, url: str, budget: float) -> BaseException:
    """``stall`` — or, once the turn has waited out its whole stall budget,
    the LLM issue that ends it (no re-send, no fallback, no persist round):
    more of the same silence will not answer it."""
    spent = _health().stalled_seconds()
    if budget <= 0 or spent < budget:
        stall.budget_left = (budget - spent) if budget > 0 else None
        return stall
    from aiforge_core.llm.model_outage import LLMStallBudget
    what = ("sent only keep-alives, no answer" if stall.keepalive
            else "kept losing the connection" if stall.phase == "dead"
            else "stopped sending mid-answer" if stall.phase == "idle"
            else "never started answering")
    n = _sends()
    return LLMStallBudget(url, n, stall, (
        f"the model at {url or '?'} {what} — {_fmt(spent)} of silence in a "
        f"row this turn, over {n} stalled send{'s' if n != 1 else ''}. The "
        "connection may be dropping long requests (a Docker / WSL network or "
        "a proxy that closes idle connections), or the prompt is too big for "
        "the model to start in time. To wait longer, set "
        "AIFORGE_LLM_STALL_BUDGET_S (seconds) in aiforge.env / the container "
        "environment and restart."))


def _sends() -> int:
    return _health().stalled_sends() or 1


def _fmt(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


__all__ = ["LLMConnectionDead", "LLMStreamStalled", "StreamWatch", "first_token_s", "idle_s"]
