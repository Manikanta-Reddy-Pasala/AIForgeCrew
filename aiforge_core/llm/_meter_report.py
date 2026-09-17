"""The meter's windows and the snapshots the UI reads: per-minute buckets,
failures, tokens and the rate-limit state."""
from __future__ import annotations

import bisect
import time

from ._meter_keys import (
    _key,
    _slot,
)


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.llm.call_meter as package
    return package


def _trim_recent_locked(now: float) -> None:
    """Keep the raw ring to the last 60 seconds. Destructive, cheap, and the
    reason the per-minute rate stays exact and O(1)-ish to read."""
    pkg = _pkg()
    cutoff = now - pkg._WINDOW_S
    while pkg._recent and pkg._recent[0] < cutoff:
        pkg._recent.popleft()
    # Same window, sorted list (see `_recent_fail`): drop the aged-out head.
    i = bisect.bisect_left(pkg._recent_fail, cutoff)
    if i:
        del pkg._recent_fail[:i]


def _mkey(ts: float) -> int:
    return int(ts // _pkg()._MINUTE_S)


def _new_bucket() -> dict:
    # `ti`/`to` = tokens in / out for the minute; `outs` = out-tokens by role,
    # which is the breakdown that names WHICH agent is writing an essay.
    return {"n": 0, "f": 0, "ti": 0, "to": 0, "roles": {}, "provs": {},
            "models": {}, "fails": {}, "outs": {}}


def _bump_bucket_locked(ts: float, role, provider, model) -> None:
    pkg = _pkg()
    key = _mkey(ts)
    b = pkg._buckets.get(key)
    if b is None:
        b = _new_bucket()
        pkg._buckets[key] = b
        # Drop everything older than the hour. Bounded by construction: at
        # most _BUCKETS + 1 slots, whatever the call rate. Evict by minute KEY,
        # not by insertion order — the two differ the moment a caller passes an
        # out-of-order `now` (tests do), and evicting the wrong slot silently
        # deletes live minutes.
        while len(pkg._buckets) > pkg._BUCKETS + 1:
            pkg._buckets.pop(min(pkg._buckets), None)
    b["n"] += 1
    for field, val in (("roles", role), ("provs", provider),
                       ("models", model)):
        v = str(val or "").strip()[:pkg._LABEL_MAX]
        if not v:
            continue
        slot = b[field]
        if v in slot or len(slot) < pkg._LABELS_PER_BUCKET:
            slot[v] = slot.get(v, 0) + 1
        else:
            slot[pkg._OVERFLOW_BUCKET] = slot.get(pkg._OVERFLOW_BUCKET, 0) + 1


def _bump_fail_bucket_locked(ts: float, reason) -> None:
    """Charge one failure to the minute of its SEND. Creates the bucket if the
    minute has no successful send in it (a minute in which every attempt failed
    is the most important minute the meter can show)."""
    pkg = _pkg()
    key = _mkey(ts)
    b = pkg._buckets.get(key)
    if b is None:
        b = _new_bucket()
        pkg._buckets[key] = b
        while len(pkg._buckets) > pkg._BUCKETS + 1:
            pkg._buckets.pop(min(pkg._buckets), None)
    b["f"] = int(b.get("f") or 0) + 1
    v = str(reason or "").strip()[:pkg._LABEL_MAX] or "error"
    fails = b.setdefault("fails", {})
    if v in fails or len(fails) < pkg._LABELS_PER_BUCKET:
        fails[v] = fails.get(v, 0) + 1
    else:
        fails[pkg._OVERFLOW_BUCKET] = fails.get(pkg._OVERFLOW_BUCKET, 0) + 1


def _prune_buckets_locked(now: float) -> None:
    """Forget minutes that have aged out — a process idle for a day must not
    report yesterday's burst as "the last hour"."""
    pkg = _pkg()
    oldest = _mkey(now) - pkg._BUCKETS
    for key in [k for k in pkg._buckets if k < oldest]:
        pkg._buckets.pop(key, None)


def _bucket_sum_locked(now: float, minutes: int) -> int:
    """Calls in the last ``minutes`` whole minutes, current partial one
    included. Minute-aligned by construction: "last 15 min" covers between 14
    and 15 minutes of wall clock — the honest cost of an O(60) read."""
    first = _mkey(now) - (minutes - 1)
    return sum(b["n"] for k, b in _pkg()._buckets.items() if k >= first)


def _fail_sum_locked(now: float, minutes: int) -> int:
    """Failures over the same window as :func:`_bucket_sum_locked`."""
    first = _mkey(now) - (minutes - 1)
    return sum(int(b.get("f") or 0) for k, b in _pkg()._buckets.items() if k >= first)


def _token_sums_locked(now: float, minutes: int) -> "tuple[int, int]":
    first = _mkey(now) - (minutes - 1)
    ti = to = 0
    for k, b in _pkg()._buckets.items():
        if k < first:
            continue
        ti += int(b.get("ti") or 0)
        to += int(b.get("to") or 0)
    return ti, to


def _tokens_by_role_locked(now: float, minutes: int) -> dict:
    first = _mkey(now) - (minutes - 1)
    out: dict = {}
    for k, b in _pkg()._buckets.items():
        if k < first:
            continue
        for name, n in (b.get("outs") or {}).items():
            out[name] = out.get(name, 0) + n
    return out


def _fail_reasons_locked(now: float, minutes: int) -> dict:
    first = _mkey(now) - (minutes - 1)
    out: dict = {}
    for k, b in _pkg()._buckets.items():
        if k < first:
            continue
        for name, n in (b.get("fails") or {}).items():
            out[name] = out.get(name, 0) + n
    return out


def _breakdown_locked(now: float, minutes: int) -> "tuple[dict, dict, dict]":
    """(by_role, by_provider, by_model) over the last ``minutes`` minutes."""
    first = _mkey(now) - (minutes - 1)
    roles: dict = {}
    provs: dict = {}
    models: dict = {}
    for k, b in _pkg()._buckets.items():
        if k < first:
            continue
        for src, dst in ((b["roles"], roles), (b["provs"], provs),
                         (b["models"], models)):
            for name, n in src.items():
                dst[name] = dst.get(name, 0) + n
    return roles, provs, models


def _series_locked(now: float, field: str = "n") -> list:
    """Requests (``n``) or failures (``f``) per minute for the last hour,
    oldest → newest. Same index in both series is the same minute."""
    pkg = _pkg()
    newest = _mkey(now)
    first = newest - (pkg._BUCKETS - 1)
    return [int((pkg._buckets.get(k) or {}).get(field) or 0)
            for k in range(first, newest + 1)]


def _per_minute_locked(now: float) -> int:
    _trim_recent_locked(now)
    return len(_pkg()._recent)


def _fail_per_minute_locked(now: float) -> int:
    _trim_recent_locked(now)      # trims both rings
    return len(_pkg()._recent_fail)


def snapshot(session_id=None) -> dict:
    """Live counts: this turn, this session, this process, and the rate over
    the last minute (across ALL sessions — the machine's load is what the user
    feels, not one chat's share of it)."""
    pkg = _pkg()
    sid = _key(session_id)
    with pkg._lock:
        # Clock read INSIDE the lock: sampled outside, a call appended while
        # this reader waited would carry a timestamp NEWER than `now` and fall
        # out of the newest bucket.
        now = time.monotonic()
        slot = _slot(sid) if sid is not None and sid in pkg._sessions else None
        _prune_buckets_locked(now)
        s = slot or {}
        return {
            "turn": int(s.get("turn") or 0),
            "session": int(s.get("total") or 0),
            "total": pkg._total,
            "per_minute": _per_minute_locked(now),
            "last_15m": _bucket_sum_locked(now, 15),
            "last_60m": _bucket_sum_locked(now, pkg._BUCKETS),
            "by_role": dict(s.get("by_role") or {}),
            # Failures are a SUBSET of the counts above, never a separate
            # population: `turn` is every attempt this message made and
            # `turn_failed` is how many of them came back with nothing.
            "turn_failed": int(s.get("turn_failed") or 0),
            "session_failed": int(s.get("failed") or 0),
            "failed": pkg._fail_total,
            "failed_per_minute": _fail_per_minute_locked(now),
            # What the model actually WROTE for this message and this chat —
            # the number a "be brief" instruction is meant to move, and the one
            # the request count cannot show (40 one-line steps and one
            # 6000-token essay are both "41 requests").
            "turn_tokens_out": int(s.get("turn_tokens_out") or 0),
            "session_tokens_out": int(s.get("tokens_out") or 0),
            "turn_tokens_in": int(s.get("turn_tokens_in") or 0),
            "session_tokens_in": int(s.get("tokens_in") or 0),
        }


def global_snapshot(*, series: bool = True) -> dict:
    """Machine-wide request meter — every LLM call this process has sent,
    whoever asked for it (chat, pipeline, jobs, the memory daemon).

    ``per_minute`` / ``last_15m`` / ``last_60m`` are ROLLING windows, NOT
    cumulative buckets: each counts the calls whose age is within it, so a
    quiet hour reads 0 even when ``total`` is large.
    """
    # Read the ceiling BEFORE taking the lock: it resolves a setting, which
    # stats (and mkdirs) the config dir. Doing that under the lock put two
    # filesystem syscalls in front of every `record()` — on the LLM hot path,
    # on a config dir that may be network-mounted.
    pkg = _pkg()
    _limits = _limit_state()
    with pkg._lock:
        now = time.monotonic()
        _prune_buckets_locked(now)
        by_role, by_provider, by_model = _breakdown_locked(now, pkg._BUCKETS)
        out = {
            "total": pkg._total,
            "per_minute": _per_minute_locked(now),
            "last_15m": _bucket_sum_locked(now, 15),
            "last_60m": _bucket_sum_locked(now, pkg._BUCKETS),
            # How many of those attempts failed, over the SAME windows — a
            # subset of the numbers above, not a second population. A rate that
            # hid them would read lowest exactly when the box is in trouble.
            "failed": pkg._fail_total,
            "failed_per_minute": _fail_per_minute_locked(now),
            "failed_15m": _fail_sum_locked(now, 15),
            "failed_60m": _fail_sum_locked(now, pkg._BUCKETS),
            "by_fail_reason": _fail_reasons_locked(now, pkg._BUCKETS),
            # Tokens as REPORTED by the provider, over the same windows.
            "tokens_in": pkg._tokens_in_total,
            "tokens_out": pkg._tokens_out_total,
            "tokens_out_15m": _token_sums_locked(now, 15)[1],
            "tokens_out_60m": _token_sums_locked(now, pkg._BUCKETS)[1],
            "tokens_in_60m": _token_sums_locked(now, pkg._BUCKETS)[0],
            "tokens_out_by_role": _tokens_by_role_locked(now, pkg._BUCKETS),
            "by_role": by_role,
            "by_provider": by_provider,
            "by_model": by_model,
            "uptime_s": round(now - pkg._started, 1),
            # The operator's ceiling and how many callers are parked on it.
            # Without these a throttled box looks broken rather than capped —
            # the meter is where someone goes to ask "why is this slow".
            **_limits,
            # An ACTUAL loss, and only within the window it can affect: the
            # 60s ring evicted calls that would otherwise be in `per_minute`.
            "rate_capped": (now - pkg._dropped_at) < pkg._WINDOW_S if pkg._dropped_at else False,
        }
        if series:
            out["series_60m"] = _series_locked(now)
            out["series_fail_60m"] = _series_locked(now, "f")
            # Tokens per minute, same 60 slots and indexes — so a reader (or a
            # test) can say WHICH minute wrote them, not just how many.
            out["series_token_out_60m"] = _series_locked(now, "to")
    return out


def _limit_state() -> dict:
    try:
        from aiforge_core.llm import rate_limiter as _rl
        return {"limit_rpm": int(_rl.global_rpm()), "queued": _rl.waiting(),
                # What the CEILING has counted in the last 60s. Not the same
                # number as `per_minute`: the ceiling also counts sends the
                # meter never sees a token for, and it is what decides whether
                # the next call queues.
                "limit_used": _rl.global_used(),
                # Seconds left on a hold the SERVER imposed (a 429/quota
                # rejection). Distinct from `queued`: that is our own ceiling
                # throttling us, this is the provider having refused.
                "held_s": round(_rl.held_for(), 1),
                # WHICH window the two numbers above describe. They are
                # machine-wide when the cross-process store is live and
                # process-local when it has fallen back — and the fallback is
                # exactly the failure an operator is trying to diagnose, so an
                # unlabelled number is the one thing that cannot help them.
                # NOTE `queued` stays process-local: it counts THIS process's
                # parked callers, which is what a user staring at this tab is
                # waiting on.
                "limit_scope": _rl.window_scope()}
    except Exception:  # noqa: BLE001 — the meter must never raise
        return {"limit_rpm": 0, "queued": 0, "limit_used": 0, "held_s": 0.0,
                "limit_scope": "process"}
