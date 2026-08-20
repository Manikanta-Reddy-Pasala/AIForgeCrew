"""Token-bucket rate limiter for cloud LLM providers.

Each provider declares its rate limits (requests-per-minute,
tokens-per-minute) via :meth:`Provider.rate_limits`. The shared
:func:`acquire` blocks the caller until budget is available rather
than raising — long-running flows queue cleanly under quota
pressure instead of dying with HTTP 429.

Two buckets per provider:
- *RPM* — request count per 60 seconds.
- *TPM* — estimated input+output tokens per 60 seconds.

Env override: ``AIFORGE_<PROVIDER>_RPM`` /
``AIFORGE_<PROVIDER>_TPM`` — operator can tighten or loosen at
runtime via the Settings UI without touching code.

Local providers (mlx-lm) opt out by returning ``rate_limits=None``;
:func:`acquire` short-circuits to a no-op for them.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("aiforge.rate_limiter")
from dataclasses import dataclass
from typing import Optional


@dataclass
class _Bucket:
    """Token bucket. Refills at ``rate`` tokens per second; cap at ``capacity``."""
    capacity: float
    rate: float
    tokens: float
    last: float

    def take(self, n: float) -> float:
        """Drain ``n`` tokens. Return seconds-to-wait when empty.

        Caller decides whether to sleep or fail.
        """
        now = time.time()
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        deficit = n - self.tokens
        return deficit / max(self.rate, 1e-9)


# Locks keyed by provider name so concurrent doer/feedback/learner
# calls don't trample each other's bucket math.
_LOCKS: dict[str, threading.Lock] = {}
# Callers currently parked on the global ceiling.
_WAIT_LOCK = threading.Lock()
_waiting = 0
_RPM_BUCKETS: dict[str, _Bucket] = {}
_TPM_BUCKETS: dict[str, _Bucket] = {}


def _bucket(store: dict[str, _Bucket], name: str,
            per_minute: float) -> _Bucket:
    """Get or create the named bucket. ``per_minute=0`` means no limit
    (caller skips the take call)."""
    if name not in store:
        cap = per_minute
        store[name] = _Bucket(
            capacity=cap, rate=cap / 60.0,
            tokens=cap, last=time.time(),
        )
    return store[name]


def _resolved_limits(provider: str,
                     declared: dict | None) -> tuple[float, float]:
    """Combine env override + provider-declared limits.

    Returns ``(rpm, tpm)``; either may be 0 to mean unlimited.
    """
    if declared is None:
        return 0.0, 0.0
    name_up = provider.upper()
    rpm = float(
        os.environ.get(f"AIFORGE_{name_up}_RPM")
        or declared.get("rpm", 0)
    )
    tpm = float(
        os.environ.get(f"AIFORGE_{name_up}_TPM")
        or declared.get("tpm", 0)
    )
    return rpm, tpm


# The operator's OWN ceiling, across every provider and every caller: chat,
# the team pipeline, jobs and the memory daemon share one bucket. The
# per-provider limits above are what a PROVIDER declares it will serve; this is
# what the person running the box is willing to send.
_GLOBAL = "__aiforge_global__"


def global_rpm() -> float:
    """Operator-set ceiling on model requests per minute; 0 = no ceiling.

    Resolves stored setting → env → default, like every other runtime knob.
    """
    try:
        from aiforge_core.config import runtime_settings as _rs
        return max(0.0, float(_rs.get("llm_max_rpm")))
    except Exception:  # noqa: BLE001 — never let a settings read block a call
        raw = os.environ.get("AIFORGE_LLM_MAX_RPM")
        try:
            return max(0.0, float(raw)) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0


def waiting() -> int:
    """How many callers are queued on the global ceiling right now — the
    number that turns "why is this slow" into "you capped it"."""
    with _WAIT_LOCK:
        return _waiting


def acquire_global(*, max_wait_s: float = 120.0) -> float:
    """Block until the operator's global rate ceiling allows one more request.

    Returns the seconds spent waiting (0 when uncapped).

    THIS ONE DELAYS, IT NEVER FAILS. The per-provider :func:`acquire` raises
    when it gives up, because a provider limit is the provider's rule and
    exceeding it earns a 429. This ceiling is the operator's own preference,
    and raising on it turned every short-deadline caller into a guaranteed
    failure: the routers and classifiers run with 15-30s budgets, so at a low
    ceiling they would have failed 100% of the time after the first minute —
    and one throttled call would have failed a whole pipeline run. So the wait
    is bounded by ``max_wait_s``, and reaching that bound lets the call through
    with a warning instead of killing it. A ceiling that stops the product is
    not a throttle, it is an outage.
    """
    global _waiting
    rpm = global_rpm()
    if rpm <= 0:
        return 0.0
    lock = _LOCKS.setdefault(_GLOBAL, threading.Lock())
    waited = 0.0
    with _WAIT_LOCK:
        _waiting += 1
    try:
        while True:
            with lock:
                b = _bucket(_RPM_BUCKETS, _GLOBAL, rpm)
                # The ceiling is live: an operator who raises it mid-run should
                # not wait out a bucket sized by the old value.
                if b.capacity != rpm:
                    b.capacity = rpm
                    b.rate = rpm / 60.0
                    b.tokens = min(b.tokens, rpm)
                sleep_s = b.take(1.0)
                if sleep_s == 0.0:
                    return waited
            if waited + sleep_s > max_wait_s:
                log.warning(
                    "llm.rate_ceiling_overrun: waited %.1fs of a %.1fs budget "
                    "at llm_max_rpm=%g — letting this call through rather than "
                    "failing it. Raise the ceiling in Settings → Agent limits "
                    "if this is common.", waited, max_wait_s, rpm)
                return waited
            time.sleep(min(sleep_s, 5.0))
            waited += min(sleep_s, 5.0)
    finally:
        with _WAIT_LOCK:
            _waiting -= 1


def acquire(provider: str, *,
            declared: dict | None,
            tokens_estimate: int = 0,
            max_wait_s: float = 120.0) -> float:
    """Block until ``provider``'s rate budget allows one more call.

    Returns the seconds spent waiting. Raises ``TimeoutError`` if
    waiting would exceed ``max_wait_s``.
    """
    rpm, tpm = _resolved_limits(provider, declared)
    if rpm <= 0 and tpm <= 0:
        return 0.0
    lock = _LOCKS.setdefault(provider, threading.Lock())
    waited = 0.0
    while True:
        with lock:
            sleeps: list[float] = []
            deducted: list[tuple] = []   # (bucket, amount) that actually drained
            if rpm > 0:
                b = _bucket(_RPM_BUCKETS, provider, rpm)
                s = b.take(1.0)
                sleeps.append(s)
                if s == 0.0:
                    deducted.append((b, 1.0))
            if tpm > 0 and tokens_estimate > 0:
                b = _bucket(_TPM_BUCKETS, provider, tpm)
                s = b.take(float(tokens_estimate))
                sleeps.append(s)
                if s == 0.0:
                    deducted.append((b, float(tokens_estimate)))
            if all(s == 0.0 for s in sleeps):
                return waited
            # One bucket had room and DEDUCTED, but another blocked us → we'll
            # sleep + retry. REFUND the drained bucket(s), else each retry
            # re-charges them and the RPM budget bleeds down while we wait on TPM
            # (the old "no refund needed" comment was wrong — it ignored retries).
            for b, amt in deducted:
                b.tokens = min(b.capacity, b.tokens + amt)
            sleep_s = max(sleeps)
        if waited + sleep_s > max_wait_s:
            raise TimeoutError(
                f"rate limit on {provider}: would wait "
                f"{waited + sleep_s:.1f}s > {max_wait_s}s"
            )
        time.sleep(min(sleep_s, 5.0))
        waited += min(sleep_s, 5.0)


def state(provider: str) -> dict:
    """Snapshot of buckets for telemetry / debug output."""
    out: dict = {"provider": provider}
    if provider in _RPM_BUCKETS:
        b = _RPM_BUCKETS[provider]
        out["rpm_capacity"] = b.capacity
        out["rpm_tokens_left"] = round(b.tokens, 2)
    if provider in _TPM_BUCKETS:
        b = _TPM_BUCKETS[provider]
        out["tpm_capacity"] = b.capacity
        out["tpm_tokens_left"] = round(b.tokens, 2)
    return out
