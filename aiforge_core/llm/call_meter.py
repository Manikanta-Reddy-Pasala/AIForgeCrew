"""How many requests have we actually sent to the LLM?

The complaint this answers is "request overload": a single chat turn can fire
far more model calls than the user sent messages — every ReAct step is a call,
retries are calls, a condense is a call, a fold is a call — and none of it was
visible. This counts REQUESTS AT THE WIRE (one per HTTP attempt, retries
included, which is what a rate-limited provider counts too) and attributes them
to the chat session that caused them.

It is machine-wide as well as chat-wide: the background daemon (compaction
folds, scope classification, jobs) sends requests nobody is watching, and those
are exactly the ones that make an interactive turn feel slow. ``global_snapshot``
serves the whole process over three rolling windows — the last minute, 15
minutes and hour — which is what the toolbar meter shows.

Deliberately in-process and bounded: this is a live meter for the UI, not
billing. Counters reset when the API restarts, and only the most recent
``_MAX_SESSIONS`` sessions and ``_RECENT_MAX`` calls are kept.
"""
from __future__ import annotations

import contextvars
import threading
import time
from collections import OrderedDict, deque

_MAX_SESSIONS = 200
# TWO structures, because the two questions have different shapes.
#
# `_recent` — raw timestamps for the last 60s only, destructively trimmed on
# every touch, exactly as before: the per-minute rate must be a true SLIDING
# window (it is the number that turns red) and it is read on EVERY ReAct step,
# so it has to stay cheap and exact.
#
# `_buckets` — one slot per WALL MINUTE for the last hour, incremented on
# write. The wider windows, the sparkline and the by-role/by-provider
# breakdowns are then O(60) to read instead of a full scan of an hour of
# calls under the lock — and the lock is the same one `record()` takes right
# before every POST, so a scan there stalls the LLM hot path precisely when
# the system is busiest (which is when someone opens this meter).
_RECENT_MAX = 20_000
_MINUTE_S = 60.0
# Distinct role/provider/model names kept PER MINUTE, and the length of each.
# Model ids are operator-editable and mlx-lm's are full filesystem paths, so
# without a cap one bad minute ships a six-figure JSON blob to every polling
# browser — assembled under the lock every LLM call takes — to render 8 rows.
_LABEL_MAX = 64
_LABELS_PER_BUCKET = 40
_BUCKETS = 60                     # minutes of history kept
_RETAIN_S = _BUCKETS * _MINUTE_S
_WINDOW_S = 60.0

# The windows the meter reports, in MINUTES of bucket history (per_minute is
# the exact sliding one and is handled separately).
WINDOWS: "dict[str, float]" = {
    "per_minute": 60.0,
    "last_15m": 900.0,
    "last_60m": 3600.0,
}

_lock = threading.Lock()
# Which TURN a call belongs to. Inherited by any context copy (the generation
# thread), so a cancelled turn's abandoned retries cannot land on the next one.
_TURN_EPOCH: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "aiforge_turn_epoch", default=None)
_total = 0
_recent: deque = deque(maxlen=_RECENT_MAX)
# minute index -> {"n": int, "roles": {...}, "provs": {...}, "models": {...}}
_buckets: "OrderedDict[int, dict]" = OrderedDict()
# When the 60s ring last had to evict a call it had not yet counted. A
# timestamp, not a counter: a drop at 09:00 says nothing about the rate at
# 14:00, and a sticky flag left the UI warning "these numbers are a floor" for
# the life of the process. Only the per-minute rate can be affected — the
# minute buckets never evict inside their window.
_dropped_at = 0.0
_sessions: "OrderedDict[str, dict]" = OrderedDict()
_started = time.monotonic()


def _key(session_id) -> "str | None":
    return None if session_id in (None, "") else str(session_id)


def _slot(sid: str) -> dict:
    slot = _sessions.get(sid)
    if slot is None:
        slot = {"total": 0, "turn": 0, "by_role": {}, "epoch": 0}
        _sessions[sid] = slot
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)     # oldest out
    else:
        _sessions.move_to_end(sid)
    return slot


def record(role: str | None = None, session_id=None, *,
           provider: str | None = None, model: str | None = None,
           now: float | None = None) -> None:
    """One request went out. Never raises — metering must not break a call.

    ``now`` is a test seam. Production always passes ``None`` and gets
    ``time.monotonic()``, which never goes backwards; the 60s ring's trim
    assumes that ordering, so feeding it decreasing stamps by hand skews the
    per-minute rate (the minute buckets are keyed and stay correct).
    """
    global _total, _dropped_at
    try:
        sid = _key(session_id)
        epoch = None
        if sid is None or not role:
            # The wire-level caller (_post) knows neither — both ride the
            # request context, which the chat turn binds and the generation
            # thread inherits (see _complete_cancellable).
            #
            # CONTEXTVAR ONLY. request_context.get_session_id() also falls back
            # to the process-global AIFORGE_CURRENT_SESSION env var, which the
            # chat route sets and never clears — so every bare thread in the
            # system (session folds, learners, classifiers) would bill its
            # calls to whichever chat last ran a turn. An unattributed call is
            # correct; a call billed to an innocent chat is not.
            try:
                from aiforge_core.runtime import request_context
                if sid is None:
                    sid = _key(request_context.context_session_id())
                role = role or request_context.get_role()
            except Exception:  # noqa: BLE001
                pass
        try:
            epoch = _TURN_EPOCH.get()
        except Exception:  # noqa: BLE001
            epoch = None
        with _lock:
            # Stamped INSIDE the lock: two threads racing between "read clock"
            # and "append" can invert the deque, and the trim below stops at
            # the first entry inside the window — one stale entry parked behind
            # a newer one would freeze the whole trim.
            _total += 1
            _ts = time.monotonic() if now is None else now
            _trim_recent_locked(_ts)
            if len(_recent) == _RECENT_MAX:
                _dropped_at = _ts    # the append below evicts the oldest
            _recent.append(_ts)
            _bump_bucket_locked(_ts, role, provider, model)
            if sid is not None:
                slot = _slot(sid)
                slot["total"] += 1
                # A cancelled generation is ABANDONED, not stopped: its thread
                # keeps retrying with the turn's context still bound. Those
                # calls belong to the turn that made them, not to whatever the
                # user typed next — so the per-turn counter only accepts calls
                # stamped with the CURRENT turn's epoch (None = a caller
                # outside any turn, e.g. a background fold).
                if epoch is None or epoch == slot["epoch"]:
                    slot["turn"] += 1
                if role:
                    slot["by_role"][role] = slot["by_role"].get(role, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def turn_reset(session_id):
    """Start a new turn for this session — the per-turn counter goes back to 0
    while the session total keeps climbing.

    Returns a token to pass to :func:`bind_turn` (or None). Call this at the
    START of the turn, in the route, BEFORE the enhancer/classifier calls: they
    are requests the user's message caused, and resetting later erased them.
    """
    sid = _key(session_id)
    if sid is None:
        return None
    with _lock:
        slot = _slot(sid)
        slot["turn"] = 0
        slot["epoch"] += 1
        return (sid, slot["epoch"])


def bind_turn(token):
    """Stamp this context (and any thread that copies it) with the turn the
    calls belong to. Returns a contextvars Token, or None."""
    if not token:
        return None
    try:
        return _TURN_EPOCH.set(token[1])
    except Exception:  # noqa: BLE001
        return None


def reset_turn(cv_token) -> None:
    if cv_token is None:
        return
    try:
        _TURN_EPOCH.reset(cv_token)
    except Exception:  # noqa: BLE001
        pass


def _trim_recent_locked(now: float) -> None:
    """Keep the raw ring to the last 60 seconds. Destructive, cheap, and the
    reason the per-minute rate stays exact and O(1)-ish to read."""
    cutoff = now - _WINDOW_S
    while _recent and _recent[0] < cutoff:
        _recent.popleft()


def _mkey(ts: float) -> int:
    return int(ts // _MINUTE_S)


def _bump_bucket_locked(ts: float, role, provider, model) -> None:
    key = _mkey(ts)
    b = _buckets.get(key)
    if b is None:
        b = {"n": 0, "roles": {}, "provs": {}, "models": {}}
        _buckets[key] = b
        # Drop everything older than the hour. Bounded by construction: at
        # most _BUCKETS + 1 slots, whatever the call rate. Evict by minute KEY,
        # not by insertion order — the two differ the moment a caller passes an
        # out-of-order `now` (tests do), and evicting the wrong slot silently
        # deletes live minutes.
        while len(_buckets) > _BUCKETS + 1:
            _buckets.pop(min(_buckets), None)
    b["n"] += 1
    for field, val in (("roles", role), ("provs", provider),
                       ("models", model)):
        v = str(val or "").strip()[:_LABEL_MAX]
        if not v:
            continue
        slot = b[field]
        if v in slot or len(slot) < _LABELS_PER_BUCKET:
            slot[v] = slot.get(v, 0) + 1
        else:
            slot["…other"] = slot.get("…other", 0) + 1


def _prune_buckets_locked(now: float) -> None:
    """Forget minutes that have aged out — a process idle for a day must not
    report yesterday's burst as "the last hour"."""
    oldest = _mkey(now) - _BUCKETS
    for key in [k for k in _buckets if k < oldest]:
        _buckets.pop(key, None)


def _bucket_sum_locked(now: float, minutes: int) -> int:
    """Calls in the last ``minutes`` whole minutes, current partial one
    included. Minute-aligned by construction: "last 15 min" covers between 14
    and 15 minutes of wall clock — the honest cost of an O(60) read."""
    first = _mkey(now) - (minutes - 1)
    return sum(b["n"] for k, b in _buckets.items() if k >= first)


def _breakdown_locked(now: float, minutes: int) -> "tuple[dict, dict, dict]":
    """(by_role, by_provider, by_model) over the last ``minutes`` minutes."""
    first = _mkey(now) - (minutes - 1)
    roles: dict = {}
    provs: dict = {}
    models: dict = {}
    for k, b in _buckets.items():
        if k < first:
            continue
        for src, dst in ((b["roles"], roles), (b["provs"], provs),
                         (b["models"], models)):
            for name, n in src.items():
                dst[name] = dst.get(name, 0) + n
    return roles, provs, models


def _series_locked(now: float) -> list:
    """Requests per minute for the last hour, oldest → newest."""
    newest = _mkey(now)
    first = newest - (_BUCKETS - 1)
    return [(_buckets.get(k) or {}).get("n", 0)
            for k in range(first, newest + 1)]


def _per_minute_locked(now: float) -> int:
    _trim_recent_locked(now)
    return len(_recent)


def snapshot(session_id=None) -> dict:
    """Live counts: this turn, this session, this process, and the rate over
    the last minute (across ALL sessions — the machine's load is what the user
    feels, not one chat's share of it)."""
    sid = _key(session_id)
    with _lock:
        # Clock read INSIDE the lock: sampled outside, a call appended while
        # this reader waited would carry a timestamp NEWER than `now` and fall
        # out of the newest bucket.
        now = time.monotonic()
        slot = _slot(sid) if sid is not None and sid in _sessions else None
        _prune_buckets_locked(now)
        return {
            "turn": int((slot or {}).get("turn") or 0),
            "session": int((slot or {}).get("total") or 0),
            "total": _total,
            "per_minute": _per_minute_locked(now),
            "last_15m": _bucket_sum_locked(now, 15),
            "last_60m": _bucket_sum_locked(now, _BUCKETS),
            "by_role": dict((slot or {}).get("by_role") or {}),
        }


def global_snapshot(*, series: bool = True) -> dict:
    """Machine-wide request meter — every LLM call this process has sent,
    whoever asked for it (chat, pipeline, jobs, the memory daemon).

    ``per_minute`` / ``last_15m`` / ``last_60m`` are ROLLING windows, NOT
    cumulative buckets: each counts the calls whose age is within it, so a
    quiet hour reads 0 even when ``total`` is large.
    """
    with _lock:
        now = time.monotonic()
        _prune_buckets_locked(now)
        by_role, by_provider, by_model = _breakdown_locked(now, _BUCKETS)
        out = {
            "total": _total,
            "per_minute": _per_minute_locked(now),
            "last_15m": _bucket_sum_locked(now, 15),
            "last_60m": _bucket_sum_locked(now, _BUCKETS),
            "by_role": by_role,
            "by_provider": by_provider,
            "by_model": by_model,
            "uptime_s": round(now - _started, 1),
            # An ACTUAL loss, and only within the window it can affect: the
            # 60s ring evicted calls that would otherwise be in `per_minute`.
            "rate_capped": (now - _dropped_at) < _WINDOW_S if _dropped_at else False,
        }
        if series:
            out["series_60m"] = _series_locked(now)
    return out


def reset_all() -> None:
    """Test helper — drop every counter."""
    global _total, _started, _dropped
    with _lock:
        _total = 0
        _dropped_at = 0.0
        _started = time.monotonic()
        _recent.clear()
        _buckets.clear()
        _sessions.clear()


__all__ = ["record", "turn_reset", "bind_turn", "reset_turn", "snapshot",
           "global_snapshot", "reset_all", "WINDOWS"]
