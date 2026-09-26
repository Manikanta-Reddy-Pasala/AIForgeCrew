"""Endpoint DOWN vs endpoint UP but THIS request keeps failing.

:mod:`aiforge_core.llm.model_wait` waits for a model that is down, forever by
default. That is only right while the endpoint is really down. A 70k-token
prompt on a local 27B that prefills slower than the first-token bound assumes
stalls on every send while ``GET /models`` answers the whole time; a server
that crashes on one prompt, a proxy that always answers 504 — re-sending the
same request after each "recovery" would hold the turn (or the ticket's
runner) forever. One :class:`RequestHealth` per logical request tells them
apart:

* a STALLED stream doubles the first-token and idle bounds of every later
  send of the same request (``2 ** stalls``), so a slow prefill eventually
  completes; the prefill speed seen on each endpoint is learned too
  (:func:`note_prefill`) and lengthens the first-token bound of later calls;
* a failure that is tied to the request (a stall, 408/502/504, a dropped
  connection) counts against the request only when a tiny completion to the
  same model succeeds promptly straight after it (llm/_model_probe) — a
  router's ``/models`` answering proves nothing about the model behind it; after ``AIFORGE_LLM_SAME_REQUEST_FAILS`` (default 4) in a row it is
  an LLM ISSUE (:class:`~aiforge_core.llm.model_outage.LLMRequestFailing`):
  the turn / ticket stops with a clear error. An allowed stop — the model is
  not down, the request is failing.

The server saying "busy" or "loading" (429, 503, "model is loading") is the
endpoint's state, not the request's: it never counts.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import threading

_HEALTH: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_request_health", default=None)

_MAX_DOUBLINGS = 8


def same_request_fails() -> int:
    """``AIFORGE_LLM_SAME_REQUEST_FAILS`` (default 4, at least 1)."""
    try:
        return max(1, int(os.environ.get("AIFORGE_LLM_SAME_REQUEST_FAILS")
                          or 4))
    except ValueError:
        return 4


class RequestHealth:
    """One logical request's failure record, shared by its every send."""

    def __init__(self) -> None:
        self.stalls = 0          # sends cut by a stream health bound
        self.stalls_seen = 0     # of those, already judged by a Waiter
        self.stalls_counted = 0  # of those, counted against the request
        self.fails = 0           # other failures counted against the request

    def total(self) -> int:
        """Failures counted against THIS request (the model answered a tiny
        probe promptly straight after each). A stall on a busy or dead box
        still lengthens the bounds (:meth:`scale`) but is not counted."""
        return self.stalls_counted + self.fails

    def scale(self) -> float:
        return float(2 ** min(self.stalls, _MAX_DOUBLINGS))


@contextlib.contextmanager
def bind(health: RequestHealth):
    """Sends inside the block belong to ``health``'s request."""
    tok = _HEALTH.set(health)
    try:
        yield health
    finally:
        _HEALTH.reset(tok)


def current() -> "RequestHealth | None":
    return _HEALTH.get()


def note_stall() -> None:
    """A send of the bound request stalled (called by the stream watch)."""
    h = _HEALTH.get()
    if h is not None:
        h.stalls += 1


def stall_scale() -> float:
    """How much longer the bound request's stream bounds are: 2 per stall."""
    h = _HEALTH.get()
    return h.scale() if h is not None else 1.0


# ── learned prefill speed per endpoint ─────────────────────────────────────

_PREFILL: dict = {}
_PREFILL_LOCK = threading.Lock()


def note_prefill(url: str, tokens: float, seconds: float) -> None:
    """The first token of a ``tokens``-token prompt came ``seconds`` after the
    send. Only a prompt big enough to measure counts (a small one is latency,
    not prefill)."""
    if not url or tokens < 2000 or seconds < 2.0:
        return
    tps = tokens / seconds
    with _PREFILL_LOCK:
        old = _PREFILL.get(url)
        _PREFILL[url] = tps if old is None else 0.6 * old + 0.4 * tps


def prefill_tps(url: str) -> "float | None":
    with _PREFILL_LOCK:
        return _PREFILL.get(url or "")


def _reset_for_tests() -> None:
    with _PREFILL_LOCK:
        _PREFILL.clear()


__all__ = ["RequestHealth", "bind", "current", "note_stall", "stall_scale",
           "note_prefill", "prefill_tps", "same_request_fails"]
