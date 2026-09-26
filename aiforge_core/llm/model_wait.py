"""Wait for the model to come back instead of failing the work.

"If the LLM is not available it should keep on waiting for the LLM to be
available — do not stop, in any of the modes." This is the one primitive every
mode uses for that: on an OUTAGE-class failure (see
:mod:`aiforge_core.llm.model_outage` for the classification) it blocks, probes
the endpoint cheaply (``GET <base>/models``) with backoff, and returns once the
server answers — the caller then re-sends the SAME request.

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
import contextlib
import contextvars
import logging
import os
import threading
import time
from typing import Any, Callable

from . import model_outage
from .client._errors import _LLMCancelled

log = logging.getLogger("aiforge.llm.model_wait")

_SCHEDULE = (2.0, 5.0, 10.0, 30.0)
_SLICE_S = 0.25
_SHUTDOWN = threading.Event()

# Bound per thread/task: extra cancel sources, status sinks, the no-wait flag.
_SCOPES: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "aiforge_model_wait_scopes", default=())
_SINKS: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "aiforge_model_wait_sinks", default=())
_OPTIONAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aiforge_model_wait_optional", default=False)


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


# ── scopes: cancel sources and status sinks ────────────────────────────────

@contextlib.contextmanager
def scope(cancel: "threading.Event | Callable[[], bool] | None" = None,
          reason: str = "cancelled"):
    """Cancel every model wait inside this block when ``cancel`` fires."""
    tok = _SCOPES.set(_SCOPES.get() + ((cancel, reason),))
    try:
        yield
    finally:
        _SCOPES.reset(tok)


@contextlib.contextmanager
def status_sink(fn: Callable[[dict], Any]):
    """``fn(status)`` receives every status change of waits inside the block."""
    tok = _SINKS.set(_SINKS.get() + (fn,))
    try:
        yield
    finally:
        _SINKS.reset(tok)


def bind_status_sink(fn: Callable[[dict], Any]) -> None:
    """Add ``fn`` to this thread's sinks for the rest of its life (a worker
    thread that has no ``with`` block around its call)."""
    _SINKS.set(_SINKS.get() + (fn,))


@contextlib.contextmanager
def optional():
    """Calls inside do not wait for the model — they fail at once."""
    tok = _OPTIONAL.set(True)
    try:
        yield
    finally:
        _OPTIONAL.reset(tok)


def scoped(fn: Callable, cancel=None, reason: str = "stopped") -> Callable:
    """``fn`` wrapped to run under :func:`scope` — for a worker-pool thread,
    which does not inherit the submitting thread's context (so neither its
    Stop nor its scopes): pass the cancel check explicitly."""
    def _run(*a, **k):
        with scope(cancel, reason):
            return fn(*a, **k)
    return _run


def shutdown() -> None:
    """Process is exiting: every wait ends with :class:`ModelWaitCancelled`."""
    _SHUTDOWN.set()


def _reset_for_tests() -> None:
    _SHUTDOWN.clear()


def _fired(src) -> bool:
    try:
        if src is None:
            return False
        if hasattr(src, "is_set"):
            return bool(src.is_set())
        return bool(src())
    except Exception:  # noqa: BLE001 — a broken check never cancels
        return False


def cancel_reason() -> str | None:
    """Why the wait must end now, or None to keep waiting."""
    if _SHUTDOWN.is_set():
        return "shutting down"
    for src, why in _SCOPES.get():
        if _fired(src):
            return why
    try:
        from aiforge_core.llm.client._http import _CANCEL
        if _fired(_CANCEL.get()):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import run_interrupt
        if _fired(run_interrupt._stop_event.get()):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
        if sid is not None and chat_cancel.is_cancelled(sid):
            return "stopped"
    except Exception:  # noqa: BLE001
        pass
    return None


# ── the probe ──────────────────────────────────────────────────────────────

def probe(url: str, api_key: str = "", timeout_s: float = 5.0) -> bool:
    """Does the endpoint answer at all? Any HTTP answer below 500 (other than
    429) counts: the server is there, the retried call will say the rest."""
    import urllib.error
    import urllib.request
    base = str(url or "").rstrip("/")
    if not base:
        return False
    req = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {api_key}",
                                   "Accept": "application/json"})
    ctx = None
    if base.lower().startswith("https://"):
        try:
            from aiforge_core.llm._ssl import context_for
            ctx = context_for(base)
        except Exception:  # noqa: BLE001
            ctx = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            resp.read(1 << 16)
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500 and exc.code != 429
    except Exception:  # noqa: BLE001 — refused, DNS, timeout, TLS: still down
        return False


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
                 probe_fn: Callable[[str, str], bool] | None = None) -> None:
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

    def wait(self, exc: BaseException) -> None:
        """Block until the endpoint answers. Raises ``exc`` itself when it is
        not an outage (or the bound runs out), :class:`ModelWaitCancelled` on
        a cancel."""
        if not self.waitable(exc):
            raise exc
        while True:
            gap = self._next_gap(exc)
            self._sleep_cancellable(gap, exc)
            self.waited += gap
            if self._after_probe(self._probe(self.url, self.api_key)):
                return

    async def await_wait(self, exc: BaseException) -> None:
        """:meth:`wait` for the event loop: sleeps without blocking it."""
        if not self.waitable(exc):
            raise exc
        loop = asyncio.get_running_loop()
        while True:
            gap = self._next_gap(exc)
            end = time.monotonic() + gap
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(min(_SLICE_S, left))
                self._check_cancel(exc)
            self.waited += gap
            up = await loop.run_in_executor(
                None, lambda: self._probe(self.url, self.api_key))
            if self._after_probe(up):
                return

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
    model)`` is asked only once a call has failed."""
    waiter: Waiter | None = None
    counter = _step_counter()
    spent0 = int(counter.get("n") or 0) if counter is not None else 0
    while True:
        try:
            out = call()
        except Exception as exc:
            if waiter is None:
                if endpoint is not None:
                    url, api_key, model = endpoint()
                waiter = Waiter(url, api_key=api_key, model=model, what=what)
            waiter.wait(exc)
            if counter is not None:
                counter["n"] = spent0
            continue
        if waiter is not None:
            waiter.recovered()
        return out


__all__ = ["ModelWaitCancelled", "Waiter", "call_with_wait", "cancel_reason",
           "delays", "optional", "probe", "probe_max_s", "scope", "shutdown",
           "status_sink", "bind_status_sink", "wait_max_s"]
