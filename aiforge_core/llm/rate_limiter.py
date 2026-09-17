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

Separately, :func:`acquire_global` is the OPERATOR's own ceiling across every
provider and caller — a sliding 60s window rather than a bucket, shared by
every AIForge process on the machine (see :mod:`_shared_window`; the
in-process window below is the fallback when that store is unavailable).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass  # noqa: F401  # used by the moved _Bucket's users

from ._rate_holds import (  # noqa: F401  # re-exported
    _hold_left_locked,
    _trim_locked,
    held_for,
    note_rate_limited,
    window_scope,
)
from ._rate_settings import (  # noqa: F401  # re-exported
    _ANY,
    _COMPACTION_ROLES,
    _DEFAULT_CHAT_RPM,
    _DEFAULT_COMPACTION_RPM,
    _DEFAULT_GLOBAL_RPM,
    _LOCKS,
    _RPM_BUCKETS,
    _TPM_BUCKETS,
    _WAIT_LOCK,
    _WINDOW_LOCK,
    _Bucket,
    _bucket,
    _cat_rpm,
    _category,
    _compaction_hold_cap,
    _compaction_roles,
    _hold_cap,
    _holds,
    _now,
    _resolved_limits,
    _sends,
    _setting,
    global_rpm,
)

log = logging.getLogger("aiforge.rate_limiter")
_waiting = 0


def waiting() -> int:
    """How many callers are queued on the global ceiling right now — the
    number that turns "why is this slow" into "you capped it"."""
    with _WAIT_LOCK:
        return _waiting


def _limit(rpm: float) -> int:
    """The window's integer capacity for a ceiling of ``rpm``.

    FLOORS AT 1. ``int(0.9)`` is 0, and a capacity of 0 made ``len(_sends) < 0``
    unreachable — so acquire_global fell through to ``_sends[0]`` on an empty
    list and raised IndexError into ``_post``, which does not catch it. A
    fractional ceiling reaches here from the env fallback in :func:`global_rpm`
    (``float(raw)``, no int coercion), so this is reachable, not theoretical.
    Anyone who asks for a fraction of a request per minute means "as few as
    possible", which is one.
    """
    return max(1, int(rpm))


def global_used() -> int:
    """Requests counted against the ceiling in the last 60 seconds — on this
    MACHINE when the shared window is live, else in this process."""
    sw = _shared()
    if sw is not None:
        n = sw.count()
        if n is not None:
            return n
    with _WINDOW_LOCK:
        _trim_locked(_now())
        return len(_sends)


def reset_global() -> None:
    """Test helper — forget every send, hold and parked caller."""
    global _waiting
    sw = _shared()
    if sw is not None:
        sw.close()          # a test may have moved AIFORGE_CONFIG_DIR
        sw.reset()          # no-ops when no store was ever created
    with _WINDOW_LOCK:
        _sends.clear()
        _holds.clear()
    with _WAIT_LOCK:
        # Not cosmetic: a leaked counter shows the toolbar a queue that will
        # never drain, with no way for an operator to clear it.
        _waiting = 0


def _shared():
    """The cross-process window, or None when it is off/unavailable."""
    try:
        from . import _shared_window as _sw
        if not _sw.enabled():
            return None
        return _sw
    except Exception:  # noqa: BLE001 — never let it break a call
        return None


def _take(rpm: float, cat: str, cat_rpm: float,
          _provider: "str | None") -> "tuple[bool, float]":
    """Claim one send against BOTH the global ceiling (``rpm``) and this call's
    category ceiling (``cat_rpm``). (claimed, seconds_until_room).

    Either ceiling of 0 means "unbounded on that axis". A send is claimed only
    when both have room; when blocked the wait is the longer of the binding
    windows, since both must be clear at once.

    Tries the SHARED window first — the whole point, since `run.sh` runs the
    API, the team-pipeline runner and the boot-time fold as separate processes
    that each used to get the operator's full allowance. Falls back to this
    process's own window whenever the shared store has no opinion, so a locked
    or unwritable file throttles slightly worse rather than not at all.
    """
    glimit = _limit(rpm) if rpm > 0 else 0
    climit = _limit(cat_rpm) if cat_rpm > 0 else 0
    sw = _shared()
    if sw is not None:
        got = sw.take(glimit, cat=cat, cat_limit=climit)
        if got is not None:
            return got
    with _WINDOW_LOCK:
        now = _now()
        _trim_locked(now)
        n = len(_sends)
        nc = sum(1 for _, c in _sends if c == cat) if climit > 0 else 0
        global_ok = glimit <= 0 or n < glimit
        cat_ok = climit <= 0 or nc < climit
        if global_ok and cat_ok:
            _sends.append((now, cat))
            return True, 0.0
        waits: list[float] = []
        if not global_ok:
            waits.append((_sends[0][0] + 60.0) - now)
        if not cat_ok:
            oc = next((ts for ts, c in _sends if c == cat), None)
            if oc is not None:
                waits.append((oc + 60.0) - now)
        if not waits:                       # raced empty; caller retries
            return False, 0.0
        return False, max(0.0, max(waits))


def _force_take(rpm: float, cat: str) -> None:
    """Count a send (of category ``cat``) going out past the wait budget."""
    sw = _shared()
    # USE THE RETURN VALUE. force() reports a miss precisely so it is not lost,
    # and dropping it here returned before the fallback too — so under
    # saturation 0.8% of forced sends (100% during a cooldown) were counted in
    # neither window. Under-counting is the direction that PERMITS extra sends
    # later, which is the failure this module exists to prevent.
    # An unbounded global axis (rpm<=0) must not trim the window to 1: pass a
    # cap large enough to be a no-op so the count still describes real traffic
    # (the category axis, or a later lowered ceiling, may still read it).
    cap = _limit(rpm) if rpm > 0 else 1_000_000_000
    if sw is not None and sw.force(cap, cat=cat):
        return
    with _WINDOW_LOCK:
        _sends.append((_now(), cat))
        if len(_sends) > cap:
            del _sends[:len(_sends) - cap]


def _overrun_through(waited: float, max_wait_s: float, rpm: float,
                     hold_s: float, cat: str, cat_rpm: float) -> None:
    """Let a call past its wait budget proceed, accounting for the overrun.

    A call let through past ``max_wait_s`` still left the box, so ``_force_take``
    appends it to the window like any other send: waiters decide independently,
    so several can overrun at once under load, and a window that did not count
    them would keep reporting a box at its limit as idle.
    """
    log.warning(
        "llm.rate_ceiling_overrun: waited %.1fs of a %.1fs budget "
        "at llm_max_rpm=%g, %s_rpm=%g (hold %.1fs) — letting this call "
        "through rather than failing it. Raise the ceiling in Settings -> "
        "Agent limits if this is common.",
        waited, max_wait_s, rpm, cat, cat_rpm, hold_s)
    # Count the send whenever EITHER axis is bounded — the category cap can bind
    # while the global is unlimited, and an uncounted overrun there reads as an
    # idle bucket.
    if rpm > 0 or cat_rpm > 0:
        _force_take(rpm, cat)


def _acquire_pass(provider, cat: str,
                  cat_rpm: float) -> tuple[bool, float, float, float]:
    """One evaluation of the global + category ceilings. Returns
    ``(done, sleep_s, rpm, hold_s)``: ``done`` when the call may proceed now
    (uncapped on both axes, or a slot was claimed); otherwise ``sleep_s`` is
    how long to park before re-evaluating.

    Re-reads the ceiling every pass so an operator who raises it mid-run is not
    made to wait out the old number. HOLD FIRST, and claim only once it is
    clear: claiming a slot and handing it back when a hold barred the send
    needed a "give one back" that, with no ownership token, deleted the newest
    row on the MACHINE — another process's real send — systematically
    under-counting real traffic during exactly the holds we are already over
    the provider's limit for.
    """
    rpm = global_rpm()
    hold_s = held_for(provider)
    window_s = 0.0
    if hold_s <= 0:
        if rpm <= 0 and cat_rpm <= 0:
            # Same rule as acquire_global's fast path: an unthrottled
            # compaction send is still counted, so the meter shows it.
            if cat == "compaction":
                _force_take(rpm, cat)
            return True, 0.0, rpm, hold_s
        claimed, window_s = _take(rpm, cat, cat_rpm, provider)
        if claimed:
            return True, 0.0, rpm, hold_s
    return False, max(hold_s, window_s), rpm, hold_s


def acquire_global(*, max_wait_s: float = 120.0,
                   provider: "str | None" = None,
                   role: "str | None" = None) -> float:
    """Block until the operator's rate ceilings allow one more request.

    ``role`` selects the category sub-ceiling: memory/compaction roles (see
    :data:`_COMPACTION_ROLES`) count against the small ``compaction_rpm``
    bucket, everything else against ``chat_rpm``; both also count against the
    machine-wide ``llm_max_rpm``. A call is released only when both its category
    and the global window have room.

    Returns the seconds spent waiting (0 when uncapped and unheld).

    A SLIDING WINDOW, not a token bucket. The bucket this replaced started
    FULL and refilled continuously, so a ceiling of N allowed an opening burst
    of N *plus* the N that refilled during the same 60 seconds — up to 2N in a
    wall-clock minute. Providers that publish "N requests per minute" count a
    sliding window, so a ceiling set deliberately UNDER the provider's (17
    against a limit of 20) still earned rejections — precisely the failure this
    ceiling exists to prevent. Counting the sends of the last 60 seconds means
    "never more than N in any minute", which is the server's own rule.

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

    THE OVERRUN IS ACCOUNTED FOR. A call let through past ``max_wait_s`` is
    still a call that left the box, so it is appended to the window like any
    other. It has to be: waiters decide independently, so under real load
    (default ceiling 5, a turn of 10-40 calls, a 120s bound) several can
    overrun at once, and a window that did not count them would keep reporting
    a box at its limit as idle.
    """
    global _waiting
    cat = _category(role)
    cat_rpm = _cat_rpm(cat)
    # Compaction/OKF is background — it must RESPECT its ceiling, never overrun
    # it, because no user is waiting on memory folding. Give it a far larger wait
    # budget so it QUEUES for its slot instead of being let through at the
    # caller's short interactive bound. Interactive chat keeps the overrun below.
    if cat == "compaction":
        _comp_cap = _compaction_hold_cap()
        if _comp_cap > 0:
            max_wait_s = max(max_wait_s, _comp_cap)
    # PRIORITY, before any ceiling: background sends step aside while someone
    # is being served. This must run ahead of the unthrottled fast path below,
    # which returns immediately — and with no ceiling configured (the default)
    # that is the only path there is.
    yielded = _priority_gate(cat, max_wait_s)
    if yielded:
        max_wait_s = max(0.0, max_wait_s - yielded)
    if global_rpm() <= 0 and cat_rpm <= 0 and held_for(provider) <= 0:
        # Nothing throttles this call — but the window is also the toolbar's
        # METER, and background traffic must never go invisible. Chat keeps the
        # old behaviour (no ceiling asked for, no window kept); compaction does
        # not, because its own sub-ceiling is 0 by default now ("use whatever
        # chat leaves"), and uncapped AND unseen is the exact defect this
        # gateway exists to prevent.
        if cat == "compaction":
            _force_take(global_rpm(), cat)
        return yielded
    waited = yielded
    with _WAIT_LOCK:
        _waiting += 1
    try:
        while True:
            done, sleep_s, rpm, hold_s = _acquire_pass(provider, cat, cat_rpm)
            if done:
                return waited
            if sleep_s <= 0:
                continue
            if waited + sleep_s > max_wait_s:
                _overrun_through(waited, max_wait_s, rpm, hold_s, cat, cat_rpm)
                return waited
            _step = min(sleep_s, 5.0)
            time.sleep(_step)
            waited += _step
    finally:
        with _WAIT_LOCK:
            _waiting -= 1


def _priority_gate(cat: str, max_wait_s: float) -> float:
    """Interactive sends mark the endpoint busy; background sends wait while it
    is. Returns the seconds this send waited. Never raises."""
    try:
        from . import interactive_gate as _gate
        if cat == "compaction":
            return _gate.yield_to_interactive(max_wait_s)
        _gate.note_interactive()
    except Exception:  # noqa: BLE001  # priority is an optimisation, not a gate
        pass
    return 0.0


def govern_send(*, role: "str | None" = None, provider: "str | None" = None,
                model: "str | None" = None, max_wait_s: float = 120.0,
                meter: bool = True) -> "tuple[float, object]":
    """THE single gateway every model send passes through. Returns
    ``(waited_s, meter_token)`` — hand the token to :func:`call_meter.record_failure`
    if the send turns out to have failed (``None`` when ``meter=False``).

    Does the two things every send owes the operator, in the one order that
    keeps them consistent:
      1. THROTTLE — block until the global ceiling AND this call's category
         sub-ceiling (chosen from ``role``: memory/compaction 'learner' → the
         small ``compaction_rpm`` bucket, everything else → ``chat_rpm``) both
         have room. Never fails; overruns are still counted.
      2. COUNT — record the request in the toolbar meter (skip with
         ``meter=False`` for a path that meters at a different point, e.g. the
         ADK path counts after the response so it can attach token usage).

    THE SEND PATHS ALL ROUTE THROUGH HERE:
      * ``llm.client._http`` — the chat / wire path
      * ``integrations.instructor_adapter`` — structured extractions (this is
        the OKF / memory-compaction path, all on the 'learner' role)
      * ``runtime.escalating_llm`` — the ADK team-pipeline path
      * ``runtime.pr_reviewer`` — the per-PR review call, which reaches litellm
        directly rather than through ``llm.client``
    A NEW path that reaches a model MUST call this too. Skipping it makes that
    traffic BOTH uncapped and invisible — the exact defect that let memory
    compaction bypass the ceiling and never show on the meter. One place to add
    a send path correctly; one place to change the policy.
    """
    waited = acquire_global(max_wait_s=max_wait_s, provider=provider, role=role)
    tok = None
    if meter:
        try:
            from aiforge_core.llm import call_meter as _meter
            tok = _meter.record(role=role, provider=provider, model=model)
        except Exception:  # noqa: BLE001 — metering must never break a call
            tok = None
    return waited, tok


def _drain_bucket(buckets: dict, provider: str, limit: float, amount: float,
                  sleeps: list, deducted: list) -> None:
    """Try to take ``amount`` from one bucket: record the wait it demands, and
    (when it had room) the deduction so a blocked sibling can refund it."""
    b = _bucket(buckets, provider, limit)
    s = b.take(amount)
    sleeps.append(s)
    if s <= 0.0:
        deducted.append((b, amount))


def _acquire_provider_pass(provider: str, rpm: float, tpm: float,
                           tokens_estimate: int) -> float:
    """One locked evaluation of ``provider``'s buckets. Returns 0.0 when the
    call may proceed (every needed bucket had room), else the seconds to sleep
    before retrying. Refunds any bucket that drained when another blocked us."""
    sleeps: list[float] = []
    deducted: list[tuple] = []   # (bucket, amount) that actually drained
    if rpm > 0:
        _drain_bucket(_RPM_BUCKETS, provider, rpm, 1.0, sleeps, deducted)
    if tpm > 0 and tokens_estimate > 0:
        _drain_bucket(_TPM_BUCKETS, provider, tpm, float(tokens_estimate),
                      sleeps, deducted)
    if all(s <= 0.0 for s in sleeps):
        return 0.0
    # One bucket had room and DEDUCTED, but another blocked us → we'll sleep +
    # retry. REFUND the drained bucket(s), else each retry re-charges them and
    # the RPM budget bleeds down while we wait on TPM (the old "no refund
    # needed" comment was wrong — it ignored retries).
    for b, amt in deducted:
        b.tokens = min(b.capacity, b.tokens + amt)
    return max(sleeps)


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
            sleep_s = _acquire_provider_pass(provider, rpm, tpm, tokens_estimate)
        if sleep_s <= 0.0:
            return waited
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
