"""Retrying a post: the retry settings, how long to wait (including a 429's
Retry-After), and the log lines for each outcome."""
from __future__ import annotations

import random
import time
import urllib.error
import urllib.request

from .. import rate_limiter as _rl
from ..types import Endpoint
from ._errors import (
    _http_err_body,
)
from ._helpers import _float_env, _int_env, _log
from ._http_stream import (
    TIMEOUT_SHIPPED_ATTR,
)


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm.client._http as package
    return package


class _RetryCfg:
    """The knobs and the deadline for one retry chain.

    Knobs:
      AIFORGE_LLM_RETRY_MAX     — total attempts per endpoint (default 3)
      AIFORGE_LLM_RETRY_BASE_S  — base backoff seconds (default 0.5)
      AIFORGE_LLM_RETRY_CAP_S   — backoff cap seconds (default 8.0)
      AIFORGE_LLM_RETRY_BUDGET  — retries must fit timeout_s x THIS (default
                                  1.5); 0 disables the budget entirely
      AIFORGE_LLM_RETRY_TIMEOUT_MAX — attempts allowed when the failure is a
                                  READ TIMEOUT (default 1 = do not re-POST)
    """

    __slots__ = ("max_attempts", "base", "cap", "timeout_max", "timeout_s",
                 "started", "budget_s", "deadline")

    def __init__(self, timeout_s: int) -> None:
        self.max_attempts = max(1, _int_env("AIFORGE_LLM_RETRY_MAX", 3))
        self.base = _float_env("AIFORGE_LLM_RETRY_BASE_S", 0.5)
        self.cap = _float_env("AIFORGE_LLM_RETRY_CAP_S", 8.0)
        self.timeout_max = max(1, _int_env("AIFORGE_LLM_RETRY_TIMEOUT_MAX", 1))
        self.timeout_s = timeout_s
        self.started = time.monotonic()
        budget_mult = _float_env("AIFORGE_LLM_RETRY_BUDGET", 1.5)
        self.budget_s = max(max(1.0, timeout_s) * budget_mult,
                            timeout_s + _pkg()._RETRY_MIN_BUDGET_S)
        self.deadline = self.started + self.budget_s if budget_mult > 0 else None

    def left(self) -> float | None:
        return (self.deadline - time.monotonic()) if self.deadline is not None else None

    def extend(self, seconds: float) -> None:
        """Give back time spent QUEUED on the operator's ceiling.

        That is not time this attempt spent failing, so it must not eat the
        retry budget: a throttled call would otherwise arrive at the retry check
        with its deadline already gone and lose retries it used to get for free.
        """
        if self.deadline is not None and seconds > 0:
            self.deadline += seconds


def _retry_after_s(exc) -> float | None:
    """The response's ``Retry-After`` in seconds, or None if it has none, or is
    unparseable."""
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _rate_limited_sleep(sleep_s: float, ra: float | None, provider: str) -> float:
    """Backoff for a 429, and the hint to the limiter that produced it.

    The provider is counting a MINUTE; a 0.5s backoff just re-earns the same
    rejection and burns another request doing it. And the rejection is the only
    ground truth we ever get about what the server is actually counting — our
    ceiling is per-process and cannot see the memory daemon — so it must reach
    the limiter whether or not THIS caller can afford to wait.

    BOUNDED. Without a cap of our own, ``Retry-After: 3600`` is two hours of
    blocking sleep on this thread from one header — and AIFORGE_LLM_RETRY_BUDGET=0
    (a documented knob) removes the deadline that would otherwise refuse it. A
    hostile or simply misconfigured gateway must not own the process. Stored
    setting -> env -> default, so Settings -> Agent limits actually moves these
    (a knob the UI cannot change is worse than one it never offered).
    """
    ra_f = max(0.0, ra) if ra is not None else 0.0
    try:
        _rl.note_rate_limited(ra_f, provider=provider)
    except Exception:  # noqa: BLE001 — never break on a hint
        pass
    rl_cap = _rl._setting("llm_rate_limit_cap_s",
                          "AIFORGE_LLM_RATE_LIMIT_CAP_S", 60.0)
    rl_back = _rl._setting("llm_rate_limit_backoff_s",
                           "AIFORGE_LLM_RATE_LIMIT_BACKOFF_S", 20.0)
    return min(max(1.0, rl_cap), max(sleep_s, ra_f or rl_back))


def _next_sleep(cfg: _RetryCfg, attempt: int, exc: Exception,
                label: str, provider: str) -> float:
    """Cost the NEXT attempt before deciding to make it — the backoff is part of
    the caller's deadline too, and a 429 Retry-After can be minutes on its own."""
    ra = _retry_after_s(exc)
    sleep_s = (min(cfg.cap, max(0.1, ra)) if ra is not None
               else min(cfg.cap, cfg.base * (2 ** (attempt - 1))))
    if label == "rate_limited":
        sleep_s = _rate_limited_sleep(sleep_s, ra, provider)
    # Jitter, to avoid a thundering herd against shared providers.
    sleep_s += random.uniform(0, 0.25)
    if label == "rate_limited" and cfg.deadline is not None:
        # Spend what this caller ACTUALLY has, not a flat 20s. The budget check
        # refuses any retry that does not leave room for a full attempt, and
        # 20s + timeout_s never fits a caller with a 15-30s budget — so the
        # routers and classifiers, the very callers acquire_global's docstring
        # names, got the flat backoff computed for them and then no retry at
        # all. Clamping to the room that is left buys them a shorter real wait
        # instead of none. (The 0.05 keeps the strict > comparison in
        # _budget_exhausted from rejecting a value that fits exactly.)
        sleep_s = max(0.0, min(
            sleep_s, cfg.deadline - time.monotonic() - float(cfg.timeout_s) - 0.05))
    return sleep_s


def _timeout_already_shipped(cfg: _RetryCfg, attempt: int, label: str,
                             retry: bool, sent: bool) -> bool:
    """The server has the request and is working on it — do not re-POST."""
    return (retry and label == "timeout" and sent
            and attempt < cfg.max_attempts and attempt >= cfg.timeout_max)


def _budget_exhausted(cfg: _RetryCfg, attempt: int, retry: bool,
                      sleep_s: float) -> bool:
    """A retry gets the FULL per-attempt timeout or it is not made."""
    return (retry and cfg.deadline is not None and attempt < cfg.max_attempts
            and time.monotonic() + sleep_s + float(cfg.timeout_s) > cfg.deadline)


def _post_with_retry(ep: Endpoint, payload: bytes, timeout_s: int,
                     *, role: str, source: str,
                     meter: "list | None" = None) -> dict:
    """Wrap _post with bounded exponential backoff on transient errors.

    On 429 with Retry-After, honour the header (capped to retry_cap).
    Permanent (4xx non-429) errors bubble immediately. See :class:`_RetryCfg`
    for the env knobs.

    A READ TIMEOUT IS NOT RETRIED by default — but only a real one. It is the
    single transient failure meaning the server ACCEPTED the request and is
    still working on it: re-POSTing leaves the first generation running and
    adds a second, so the retry worsens the overload it is retrying on. The
    rule is gated on the request having actually SHIPPED (``sent``), because a
    bare TimeoutError also comes out of the client-side rate limiter giving up
    and out of a stalled connect/TLS handshake — neither of which cost the
    server anything, and both of which must keep their retries. Set
    ``AIFORGE_LLM_RETRY_TIMEOUT_MAX`` above 1 to re-POST anyway.

    Be aware of what does NOT stand behind this: in the default self-hosted
    setup ``router._CLOUD_PROVIDERS`` is empty and ``fallback()`` has no second
    provider to offer, so a read timeout ends ``complete()`` with
    ``llm.exhausted`` rather than falling through to another endpoint. That is
    the deliberate trade — one abandoned generation beats three.

    THE BUDGET. ``timeout_s`` is the caller's deadline, not one attempt's: a
    20s route classifier retrying three read-timeouts blocks its caller for a
    full minute. So the chain is bounded by ``timeout_s * budget_mult``
    (floored at +10s so short-timeout callers keep their cheap retries) and a
    retry is made only when a FULL attempt still fits inside what is left —
    never a stub with a few seconds on it, which would manufacture exactly the
    abandoned generation this is written to avoid. The bound is per CHAIN;
    ``client.complete``'s empty-response loop can start several."""
    cfg = _RetryCfg(timeout_s)
    last: Exception | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        # Did THIS attempt get the prompt onto the wire? Decides whether a
        # timeout means "the server is working on it" or "we never reached it".
        sent = [False]
        throttled = [0.0]
        try:
            # `meter` forwarded only when a caller asked for the token: a
            # test that fakes `_post` with the old signature stays valid, and
            # the kwarg appears exactly where someone needs the token back.
            extra = {"meter": meter} if meter is not None else {}
            return _pkg()._post(ep, payload, timeout_s, role=role, sent=sent,
                         max_wait_s=cfg.left(), throttled=throttled, **extra)
        except Exception as exc:  # noqa: BLE001 — classifier handles
            retry, label = _pkg()._is_transient_exc(exc)
            last = exc
            cfg.extend(throttled[0])
            sleep_s = _next_sleep(cfg, attempt, exc, label, ep.provider)
            budget_out = _timeout_already_shipped(cfg, attempt, label, retry, sent[0])
            if budget_out:
                _log_timeout_not_retried(cfg, ep, attempt, label, exc, role, source)
            elif _budget_exhausted(cfg, attempt, retry, sleep_s):
                budget_out = True
                _log_budget_exhausted(cfg, ep, attempt, label, sleep_s, exc,
                                      role, source)
            if not retry or budget_out or attempt >= cfg.max_attempts:
                _mark_shipped_timeout(exc, label, sent[0])
                _log_transport_error(ep, attempt, label, retry, budget_out, exc,
                                     role, source)
                raise
            _log_transport_retry(ep, attempt, label, sleep_s, exc, role, source)
            time.sleep(sleep_s)
    # Defensive — loop above always either returns or raises.
    assert last is not None
    raise last


def _mark_shipped_timeout(exc: Exception, label: str, sent: bool) -> None:
    if label != "timeout" or not sent:
        return
    try:
        setattr(exc, TIMEOUT_SHIPPED_ATTR, True)
    except Exception:  # noqa: BLE001 — never break on a marker
        pass


def _log_timeout_not_retried(cfg: _RetryCfg, ep: Endpoint, attempt: int,
                             label: str, exc: Exception, role: str,
                             source: str) -> None:
    _log.info(
        "llm.timeout_not_retried provider=%s attempt=%d "
        "timeout=%ds — the server already has this request",
        ep.provider, attempt, cfg.timeout_s,
        extra={"aiforge": {"role": role, "provider": ep.provider,
                           "source": source, "attempt": attempt,
                           "label": label, "error": str(exc)[:200]}},
    )


def _log_budget_exhausted(cfg: _RetryCfg, ep: Endpoint, attempt: int,
                          label: str, sleep_s: float, exc: Exception,
                          role: str, source: str) -> None:
    _log.info(
        "llm.retry_budget_exhausted provider=%s label=%s "
        "attempt=%d elapsed=%.1fs budget=%.1fs — not retrying",
        ep.provider, label, attempt, time.monotonic() - cfg.started, cfg.budget_s,
        extra={"aiforge": {"role": role, "provider": ep.provider,
                           "source": source, "attempt": attempt,
                           "label": label, "sleep_s": round(sleep_s, 3),
                           "error": str(exc)[:200]}},
    )


def _log_transport_error(ep: Endpoint, attempt: int, label: str, retry: bool,
                         budget_out: bool, exc: Exception, role: str,
                         source: str) -> None:
    body = _http_err_body(exc)
    _log.warning(
        "llm.transport_error role=%s provider=%s model=%s "
        "url=%s/chat/completions label=%s attempt=%d err=%s%s",
        role, ep.provider, ep.model, str(ep.base_url).rstrip("/"), label,
        attempt, str(exc)[:300], f" body={body}" if body else "",
        extra={"aiforge": {"role": role, "provider": ep.provider,
                           "model": ep.model, "source": source,
                           "attempt": attempt, "label": label,
                           "fatal": not retry, "budget_exhausted": budget_out,
                           "error": (str(exc) + " " + body)[:300]}},
    )


def _log_transport_retry(ep: Endpoint, attempt: int, label: str, sleep_s: float,
                         exc: Exception, role: str, source: str) -> None:
    _log.info(
        "llm.transport_retry provider=%s url=%s label=%s attempt=%d "
        "sleep=%.2fs err=%s",
        ep.provider, str(ep.base_url).rstrip("/"), label, attempt, sleep_s,
        str(exc)[:300],
        extra={"aiforge": {"role": role, "provider": ep.provider,
                           "source": source, "attempt": attempt,
                           "label": label, "sleep_s": round(sleep_s, 3),
                           "error": str(exc)[:200]}},
    )
