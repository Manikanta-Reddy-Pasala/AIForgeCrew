"""Wait for the model to come back instead of failing the work.

"If the LLM is not available it should keep on waiting for the LLM to be
available — do not stop, in any of the modes." This is the one primitive every
mode uses for that: on an OUTAGE-class failure (see
:mod:`aiforge_core.llm.model_outage` for the classification) it blocks, probes
the MODEL with a one-token completion (llm/_model_probe — not ``/models``,
which a router answers while the model behind it is dead) with backoff, and
returns once it answers — the caller then re-sends the SAME request.

    out = model_wait.call_with_wait(lambda: do_call(), url=ep.base_url, ...)
    # or, around an existing retry loop:
    waiter = model_wait.Waiter(url, api_key=..., model=...)
    waiter.wait(exc)          # raises ``exc`` when it is not worth waiting for
    await waiter.await_wait(exc)   # the asyncio form (ADK)

Knobs:
  AIFORGE_LLM_WAIT_MAX_S        total seconds of waiting per call; 0 (default)
                                = forever; negative = do not wait at all.
  AIFORGE_LLM_WAIT_PROBE_MAX_S  longest gap between probes (default 30); the
                                gaps grow 2 s → 5 s → 10 s → 30 s.
  AIFORGE_LLM_WAIT_STATUS_S     once the gap is at its cap, repeat the status
                                line this often (default 300).

  AIFORGE_LLM_SAME_REQUEST_FAILS  the model answers a tiny probe promptly but
                                THIS request failed that many times in a row: an LLM issue, not an
                                outage — :class:`LLMRequestFailing` (default 4;
                                see llm/request_health).

Cancelled by: Stop on the chat session (the client's cancel token, the session
cancel flag, a worker's stop event), a scope bound with :func:`scope` (a ticket
whose claim was lost or that was cancelled), :func:`shutdown` (process exit).
A cancel raises :class:`ModelWaitCancelled`.

Status: every change — the wait starting, the probe gap growing, a periodic
"still waiting" at the cap, the model coming back — goes to the log and to every
sink bound with :func:`status_sink` (the chat turns it into a thought line, a
ticket into an event row). Never one per probe.

Optional side calls (a title, a next-step suggestion) run under
:func:`optional` and fail at once instead of waiting — they must never hold up
the work, and never fail it either (their callers already fall back).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable

from . import _model_probe, model_outage, request_health
from ._model_probe import (  # noqa: F401 — tests patch model_wait.probe
    live_probe,
    probe,
)
from ._wait_scope import (  # noqa: F401 — the public surface lives here too
    _HOOKS,
    _OPTIONAL,
    _SHUTDOWN,
    _SINKS,
    WAITED_ATTR,
    LLMRequestFailing,
    _reset_for_tests,
    bind_status_sink,
    bind_wait_hooks,
    cancel_reason,
    optional,
    scope,
    scoped,
    shutdown,
    side_call,
    status_sink,
    was_waited,
)
from .client._errors import _LLMCancelled

log = logging.getLogger("aiforge.llm.model_wait")

_SCHEDULE = (2.0, 5.0, 10.0, 30.0)
_SLICE_S = 0.25


class ModelWaitCancelled(_LLMCancelled):
    """The wait for the model was cancelled (Stop, ticket cancel, shutdown).

    A subclass of the client's ``_LLMCancelled``, so every layer that already
    refuses to retry a cancelled call treats this one the same."""

    def __init__(self, reason: str, url: str = "") -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"stopped waiting for the model at {url or '?'}: {reason}")


# ── settings ────────────────────────────────────────────────────────────────

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


#: When AIFORGE_LLM_WAIT_MAX_S is unset. 0 = forever. (The test suite lowers
#: it, so a test that clears the environment cannot hang on a dead port.)
_DEFAULT_MAX_S = 0.0


def wait_max_s() -> float:
    """0 = forever (the default), >0 = bound, <0 = do not wait."""
    return _env_float("AIFORGE_LLM_WAIT_MAX_S", _DEFAULT_MAX_S)


def probe_max_s() -> float:
    return max(1.0, _env_float("AIFORGE_LLM_WAIT_PROBE_MAX_S", 30.0))


def _status_every_s() -> float:
    return max(30.0, _env_float("AIFORGE_LLM_WAIT_STATUS_S", 300.0))


def delays(cap: float | None = None):
    """The probe gaps: 2, 5, 10, 30 … (never above ``cap``), then ``cap``."""
    cap = probe_max_s() if cap is None else cap
    for d in _SCHEDULE:
        if d >= cap:
            break
        yield d
    while True:
        yield cap


def _fmt(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ── the waiter ─────────────────────────────────────────────────────────────

class Waiter:
    """One call's wait state. Re-used across that call's retries, so repeated
    failures keep backing off instead of starting over at 2 s."""

    def __init__(self, url: str, *, api_key: str = "", model: str = "",
                 what: str = "", sleep: Callable[[float], None] | None = None,
                 probe_fn: Callable[[str, str], bool] | None = None,
                 health: "request_health.RequestHealth | None" = None) -> None:
        self.url = str(url or "")
        self.api_key = api_key or ""
        self.model = model or ""
        self.what = what
        self.started: float | None = None
        self.waited = 0.0
        self.probes = 0
        self._gaps = delays()
        self._gap = 0.0
        self._last_emit = 0.0
        self._last_gap = -1.0
        self._sleep = sleep
        self._probe = probe_fn or (lambda u, k: probe(u, k))
        self._probe_model = (
            (lambda u, k, m: probe_fn(u, k)) if probe_fn is not None
            else (lambda u, k, m: probe(u, k, model=m)))
        self.health = health or request_health.RequestHealth()

    # — classification ——————————————————————————————————————————————
    def waitable(self, exc: BaseException) -> bool:
        """Is ``exc`` an outage worth waiting for, here, now?"""
        bound = wait_max_s()
        if _OPTIONAL.get() or bound < 0:
            return False
        if 0 < bound < next(delays()):
            return False               # the bound is shorter than one probe gap
        if model_outage.classify(exc) != model_outage.OUTAGE:
            return False
        return not self._id_is_wrong(exc)

    def _id_is_wrong(self, exc: BaseException) -> bool:
        """"Model not loaded" from a server that DOES list models, none of them
        this one, is a wrong id — LM Studio says the same words for both."""
        if not (self.model and self.url and model_outage.names_loading(exc)):
            return False
        try:
            from aiforge_core.llm.client._models import model_is_missing
            missing = model_is_missing(self.url, self.model, self.api_key)
        except Exception:  # noqa: BLE001
            return False
        return bool(missing)

    # — status ——————————————————————————————————————————————————————
    def _status(self, state: str, next_s: float = 0.0) -> dict:
        down = time.monotonic() - (self.started or time.monotonic())
        url = self.url or "?"
        if state == "back":
            text = f"▶ the model at {url} is back after {_fmt(down)} — continuing"
        elif state == "gave_up":
            text = f"⚠ gave up waiting for the model at {url} after {_fmt(down)}"
        elif state == "cancelled":
            text = f"⏹ stopped waiting for the model at {url}"
        elif state == "resend":
            text = (f"⟳ the model at {url} is up but this request failed "
                    f"({self.health.total()}/{request_health.same_request_fails()})"
                    " — sending it again")
        else:
            text = (f"⏸ waiting for model at {url} (down {_fmt(down)}, "
                    f"next probe {_fmt(next_s)})")
        return {"type": "llm_wait", "state": state, "url": url,
                "model": self.model, "down_s": round(down, 1),
                "next_probe_s": round(next_s, 1), "probes": self.probes,
                "what": self.what, "text": text}

    def _emit(self, state: str, next_s: float = 0.0, force: bool = False) -> None:
        now = time.monotonic()
        if state == "waiting" and not force:
            changed = next_s != self._last_gap
            due = now - self._last_emit >= _status_every_s()
            if not (changed or due):
                return
        self._last_emit, self._last_gap = now, next_s
        st = self._status(state, next_s)
        log.warning("llm.wait %s", st["text"],
                    extra={"aiforge": {k: v for k, v in st.items() if k != "text"}})
        for fn in _SINKS.get():
            try:
                fn(st)
            except Exception:  # noqa: BLE001 — a sink never breaks the wait
                log.debug("llm.wait status sink failed", exc_info=True)

    def recovered(self) -> None:
        """The retried call went through: say so once, reset the backoff."""
        if self.started is not None:
            self._emit("back", force=True)
        self.started = None
        self.waited = 0.0
        self._gaps = delays()
        self._last_gap = -1.0

    # — one wait cycle ———————————————————————————————————————————————
    def _next_gap(self, exc: BaseException) -> float:
        """The next gap, or raise ``exc`` when the bound would be crossed."""
        if self.started is None:
            self.started = time.monotonic()
        why = cancel_reason()
        if why:
            self._emit("cancelled", force=True)
            raise ModelWaitCancelled(why, self.url) from exc
        gap = next(self._gaps)
        bound = wait_max_s()
        if bound > 0 and self.waited + gap > bound:
            self._emit("gave_up", force=True)
            try:
                setattr(exc, WAITED_ATTR, True)
            except Exception:  # noqa: BLE001 — an immutable exception
                pass
            raise exc
        self._gap = gap
        self._emit("waiting", gap)
        return gap

    def _after_probe(self, up: bool) -> bool:
        self.probes += 1
        if up:
            try:
                from aiforge_core.llm import endpoint_breaker
                endpoint_breaker.record_success(self.url)
            except Exception:  # noqa: BLE001
                pass
        return up

    # — endpoint up, this request failing (llm/request_health) —————————
    def _up(self) -> bool:
        """Recovery: does the model answer a tiny completion at all?"""
        if self.model:
            return self._probe_model(self.url, self.api_key, self.model)
        return self._probe(self.url, self.api_key)

    def _live(self, fresh: bool = False) -> str:
        """Judgement: the state of a tiny completion to the SAME model
        (``live_probe`` — tests patch the module attribute with a bool)."""
        got = live_probe(self.url, self.api_key, self.model, state=True,
                         fresh=fresh)
        if got is True or got is False:
            return _model_probe.OK if got else _model_probe.BUSY
        return str(got)

    def _crashed(self, exc: BaseException, resent_at: float | None) -> None:
        """The server went DOWN right after a resend it had confirmed it
        could take: one crash cycle. Raises :class:`LLMRequestFailing` once
        this request has crashed the server too often."""
        h = self.health
        if resent_at is None \
                or time.monotonic() - resent_at > request_health.crash_window_s():
            return
        h.crashes += 1
        if h.crashes < request_health.crash_resends():
            return
        log.warning("llm.request_crashes url=%s n=%d err=%.200s", self.url,
                    h.crashes, exc)
        err = LLMRequestFailing(
            self.url, h.crashes, exc,
            f"LLM issue: the model server at {self.url or '?'} went down "
            f"right after this request was sent, {h.crashes} times — the "
            "request crashes the model server")
        err.cause = "request crashes the model server"
        raise err from exc

    def _judge(self, exc: BaseException) -> bool:
        """Count ``exc`` against the request only when the MODEL is up: a
        tiny completion to it succeeds promptly straight after the failure —
        or the probe itself is refused as a client error (INCONCLUSIVE: the
        server is up; count, as the old ``/models`` check did). A router
        whose ``/models`` answers while the model behind it is dead, a box
        whose queue holds the probe too, a probe that times out — all of that
        is an outage or a busy model: waited for, never counted (unless it
        is the server crashing on this very request: :meth:`_crashed`).
        True = counted (re-send without an outage wait). Raises
        :class:`LLMRequestFailing` once the request has failed too often."""
        h = self.health
        resent_at, h.up_at = h.up_at, None
        if model_outage.explicit_busy(exc) or not model_outage.request_bound(exc):
            return False        # the endpoint's state, or a connect failure
        stall = model_outage.is_stall(exc) and h.stalls > h.stalls_seen
        if stall:
            h.stalls_seen = h.stalls      # the stream watch saw it; judge once
        state = self._live(fresh=resent_at is not None)
        if not self._after_probe(state in (_model_probe.OK,
                                           _model_probe.INCONCLUSIVE)):
            if state == _model_probe.DOWN:
                self._crashed(exc, resent_at)
            return False
        if stall:
            h.stalls_counted += 1
        else:
            h.fails += 1
        if h.total() >= request_health.same_request_fails():
            log.warning("llm.request_fails url=%s n=%d err=%.200s", self.url,
                        h.total(), exc)
            raise LLMRequestFailing(self.url, h.total(), exc) from exc
        return True

    def _resend_gap(self, exc: BaseException) -> float:
        """Before re-sending a request the endpoint failed while up: none after
        a stall (its next send already waits twice as long), else a short
        backoff."""
        self._emit("resend", force=True)
        if model_outage.is_stall(exc):
            return 0.0
        return min(probe_max_s(), next(delays()) * max(1, self.health.fails))

    def _hook(self, i: int) -> None:
        for pair in _HOOKS.get():
            try:
                pair[i]()
            except Exception:  # noqa: BLE001 — a hook never breaks the wait
                log.debug("llm.wait hook failed", exc_info=True)

    def wait(self, exc: BaseException) -> bool:
        """Block until the endpoint answers. Raises ``exc`` itself when it is
        not an outage (or the bound runs out), :class:`ModelWaitCancelled` on
        a cancel, :class:`LLMRequestFailing` when the endpoint is up but this
        request keeps failing. True when the failure was counted against the
        request (endpoint up), False after a wait for a down endpoint."""
        if not self.waitable(exc):
            raise exc
        if self._judge(exc):
            self._sleep_cancellable(self._resend_gap(exc), exc)
            return True
        self._hook(0)
        try:
            while True:
                gap = self._next_gap(exc)
                self._sleep_cancellable(gap, exc)
                self.waited += gap
                if self._after_probe(self._up()):
                    self.health.up_at = time.monotonic()
                    return False
        finally:
            self._hook(1)

    async def await_wait(self, exc: BaseException) -> bool:
        """:meth:`wait` for the event loop: sleeps without blocking it."""
        if not self.waitable(exc):
            raise exc
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, self._judge, exc):
            await self._async_sleep(self._resend_gap(exc), exc)
            return True
        while True:
            gap = self._next_gap(exc)
            await self._async_sleep(gap, exc)
            self.waited += gap
            up = await loop.run_in_executor(None, self._up)
            if self._after_probe(up):
                self.health.up_at = time.monotonic()
                return False

    async def _async_sleep(self, gap: float, exc: BaseException) -> None:
        end = time.monotonic() + gap
        while True:
            self._check_cancel(exc)
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(_SLICE_S, left))

    def _check_cancel(self, exc: BaseException) -> None:
        why = cancel_reason()
        if why:
            self._emit("cancelled", force=True)
            raise ModelWaitCancelled(why, self.url) from exc

    def _sleep_cancellable(self, gap: float, exc: BaseException) -> None:
        if self._sleep is not None:
            self._check_cancel(exc)
            self._sleep(gap)
            self._check_cancel(exc)
            return
        end = time.monotonic() + gap
        while True:
            self._check_cancel(exc)
            left = end - time.monotonic()
            if left <= 0:
                return
            _SHUTDOWN.wait(min(_SLICE_S, left))


def _step_counter() -> "dict | None":
    try:
        from aiforge_core.llm import call_meter
        cur = call_meter._STEP_CALLS.get()
        return cur if isinstance(cur, dict) else None
    except Exception:  # noqa: BLE001
        return None


def call_with_wait(call: Callable[[], Any], *, url: str = "",
                   api_key: str = "", model: str = "", what: str = "",
                   endpoint: "Callable[[], tuple] | None" = None) -> Any:
    """``call()``, re-run after every outage once the endpoint answers again.

    A send that failed on the outage is not the step's to pay for: the per-step
    generation budget (call_meter's step counter) is put back after each one,
    so a model that was down for an hour does not leave the step with no
    retries for a real bad answer afterwards. ``endpoint()`` → ``(url, key,
    model)`` is asked only once a call has failed.

    Every send of the request shares one llm/request_health record: a stall
    makes the next send's stream bounds longer, and a request the endpoint
    keeps failing while UP ends in :class:`LLMRequestFailing` (those sends DO
    spend the step's budget — they were real generations)."""
    waiter: Waiter | None = None
    health = request_health.RequestHealth()
    counter = _step_counter()
    spent0 = int(counter.get("n") or 0) if counter is not None else 0
    while True:
        try:
            with request_health.bind(health):
                out = call()
        except Exception as exc:
            if waiter is None:
                if endpoint is not None:
                    url, api_key, model = endpoint()
                waiter = Waiter(url, api_key=api_key, model=model, what=what,
                                health=health)
            if waiter.wait(exc):
                spent0 = int(counter.get("n") or 0) if counter is not None else 0
            elif counter is not None:
                counter["n"] = spent0
            continue
        if waiter is not None:
            waiter.recovered()
        return out


__all__ = ["ModelWaitCancelled", "LLMRequestFailing", "Waiter", "WAITED_ATTR",
           "call_with_wait", "cancel_reason", "delays", "optional", "probe",
           "probe_max_s", "scope", "scoped", "shutdown", "side_call",
           "status_sink", "bind_status_sink", "bind_wait_hooks", "was_waited",
           "wait_max_s"]
