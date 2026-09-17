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

A request that FAILED is still a request — the provider counted it, the retry
storm it belongs to is exactly what the meter exists to make visible, and a
rate that quietly dropped failures would read low precisely when the box is in
trouble. So ``per_minute`` stays "attempts at the wire" and the failures are
counted ALONGSIDE it (``failed_per_minute``, ``by_fail_reason``): "40/min, 38
of them failing" is the reading that names the problem, and neither half of it
can be recovered from the other.

A failure is billed to the minute (and the chat turn) of its SEND, not of the
moment it surfaced — a 600s timeout that fails now was traffic ten minutes ago,
and charging it to the current minute would invent a burst that never happened
while leaving its own minute looking clean.

TOKENS are counted the same way, from what the provider reports in each
response (``usage.prompt_tokens`` / ``completion_tokens``). Requests answer
"how many calls did that cost"; tokens answer "how much did the model WRITE",
which is the question a prompt asking for shorter answers is meant to move —
and until this existed the answer was thrown away (``_record_usage`` was a
``pass``), so every claim about verbosity was a guess.

Deliberately in-process and bounded: this is a live meter for the UI, not
billing. Counters reset when the API restarts, and only the most recent
``_MAX_SESSIONS`` sessions and ``_RECENT_MAX`` calls are kept.
"""
from __future__ import annotations

import bisect
import contextvars
import threading
import time
from collections import OrderedDict, deque

from ._meter_keys import (  # noqa: F401  # re-exported
    _attribute,
    _bill_session_locked,
    _bump_step_counter,
    _current_epoch,
    _key,
    _slot,
)
from ._meter_report import (  # noqa: F401  # re-exported
    _breakdown_locked,
    _bucket_sum_locked,
    _bump_bucket_locked,
    _bump_fail_bucket_locked,
    _fail_per_minute_locked,
    _fail_reasons_locked,
    _fail_sum_locked,
    _limit_state,
    _mkey,
    _new_bucket,
    _per_minute_locked,
    _prune_buckets_locked,
    _series_locked,
    _token_sums_locked,
    _tokens_by_role_locked,
    _trim_recent_locked,
    global_snapshot,
    snapshot,
)

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
_OVERFLOW_BUCKET = "…other"  # bucket key for labels past _LABELS_PER_BUCKET
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
# A STEP's own send counter. A dict, not an int, on purpose: a generation may
# run in a copied context (the cancellable path copies it into a worker
# thread), and a copied context shares the same OBJECT — an int would be
# rebound in the copy and the parent would never see the count.
#
# Why this exists at all: the per-step generation ceiling first measured spend
# as a delta of the session's per-TURN request count, which made it inert for
# every caller without a session (jobs, text_doer, the analysis fan-out,
# parallel subtasks — precisely the unattended paths where a retry storm has
# nobody watching), refundable by a concurrent turn_reset, and spendable by any
# unrelated same-session traffic. A step counter is none of those things.
_STEP_CALLS: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "aiforge_step_calls", default=None)
_total = 0
_fail_total = 0
_tokens_in_total = 0
_tokens_out_total = 0
_recent: deque = deque(maxlen=_RECENT_MAX)
# Failure timestamps for the exact 60s window. A LIST kept sorted, not a deque:
# a failure carries the timestamp of its SEND, so failures arrive out of order
# (a 600s timeout settles long after a 5s one that started later) and appending
# an older stamp behind a newer one would park it where the popleft-trim stops
# — freezing the whole trim, which is the one bug `_recent`'s comments warn
# about. Failures are rare and the window is a minute, so the insort memmove is
# over a handful of floats.
_recent_fail: "list[float]" = []
# minute index -> {"n": int, "f": int, "roles": {...}, "provs": {...},
#                  "models": {...}, "fails": {reason: int}}
_buckets: "OrderedDict[int, dict]" = OrderedDict()
# When the 60s ring last had to evict a call it had not yet counted. A
# timestamp, not a counter: a drop at 09:00 says nothing about the rate at
# 14:00, and a sticky flag left the UI warning "these numbers are a floor" for
# the life of the process. Only the per-minute rate can be affected — the
# minute buckets never evict inside their window.
_dropped_at = 0.0
_sessions: "OrderedDict[str, dict]" = OrderedDict()
_started = time.monotonic()


def record(role: str | None = None, session_id=None, *,
           provider: str | None = None, model: str | None = None,
           now: float | None = None):
    """One request went out. Never raises — metering must not break a call.

    Returns an opaque TOKEN to hand to :func:`record_failure` if this request
    turns out to have failed, or ``None`` if nothing could be recorded. The
    token carries the send timestamp, session and turn epoch, so the failure
    lands on the minute and the message that actually paid for it however long
    the call took to give up. Callers that ignore the return value keep the
    old behaviour exactly.

    ``now`` is a test seam. Production always passes ``None`` and gets
    ``time.monotonic()``, which never goes backwards; the 60s ring's trim
    assumes that ordering, so feeding it decreasing stamps by hand skews the
    per-minute rate (the minute buckets are keyed and stay correct).
    """
    global _total, _dropped_at
    try:
        sid, role = _attribute(_key(session_id), role)
        epoch = _current_epoch()
        _bump_step_counter()
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
                epoch = _bill_session_locked(sid, role, epoch)
        return (_ts, sid, epoch)
    except Exception:  # noqa: BLE001
        return None


def _record_window_fail(ts: float, now: float, reason) -> bool:
    """Record a failure into the rate window + per-minute buckets (caller holds
    ``_lock``). Returns False when the send is older than the reported history
    (NO minute to charge to): it stays in the lifetime total but is left out of
    every window AND the caller skips the per-session update too, exactly as the
    original early-return did. The cutoff matches the 59-whole-minutes-back the
    windows read, not _RETAIN_S."""
    if _mkey(ts) < _mkey(now) - (_BUCKETS - 1):
        return False
    if now - ts < _WINDOW_S:
        # Only stamps inside the exact window matter to the rate; the rest are
        # trimmed on the next read anyway, and keeping them out bounds the insort.
        bisect.insort(_recent_fail, ts)
        del _recent_fail[:max(0, len(_recent_fail) - _RECENT_MAX)]
    _bump_fail_bucket_locked(ts, reason)
    return True


def _record_session_fail(sid, epoch) -> None:
    """Increment a KNOWN session's failed counter (caller holds ``_lock``). Only
    a session the meter already knows — a failure must not mint a slot (and evict
    a live one) for a chat that never sent anything through this process."""
    if sid is None or sid not in _sessions:
        return
    slot = _slot(sid)
    slot["failed"] = int(slot.get("failed") or 0) + 1
    # A REAL epoch match only. `record` stamps the token with the turn it counted
    # the send against, so an unstamped failure is one whose turn is unknown —
    # guessing "the current one" can put turn_failed above a turn that never
    # included the send. Undercounting one turn beats billing an innocent one.
    if epoch is not None and epoch == slot["epoch"]:
        slot["turn_failed"] = int(slot.get("turn_failed") or 0) + 1


def record_failure(token=None, reason: str | None = None, *,
                   session_id=None, now: float | None = None) -> None:
    """One request that :func:`record` counted did NOT come back with an answer.
    Never raises.

    ``token`` is what ``record`` returned for that very request, and it is
    REQUIRED: without one this is a no-op. That is what makes "failures are a
    subset of requests" structural — counting a failure for an uncounted send
    would put ``failed`` above ``total``. The token also carries the minute, chat
    and turn of the SEND, which is what the failure is billed to.

    ``reason`` is a short label (``http_500``, ``timeout``, ``empty``…), clipped
    and its per-minute set capped so an unbounded reason (a stringified
    exception) can't ship a novel to every polling browser.
    """
    global _fail_total
    try:
        if not (isinstance(token, tuple) and len(token) == 3):
            return          # no counted send → nothing to mark as failed
        ts, sid, epoch = token
        if session_id is not None:
            sid = _key(session_id)
        # NOTE: no ambient-context fallback for the session. The token already
        # answers the question, and re-reading the context at SETTLE time would
        # attribute a fold's timeout to whatever chat the thread was rebound to.
        with _lock:
            _now = time.monotonic() if now is None else now
            if not isinstance(ts, (int, float)) or ts > _now:
                # No usable stamp, or one from the future (a clock seam, or a
                # hand-fed `now` in a test): treat it as happening now.
                ts = _now
            _fail_total += 1
            if _record_window_fail(ts, _now, reason):
                _record_session_fail(sid, epoch)
    except Exception:  # noqa: BLE001
        pass


def step_begin() -> dict:
    """Start counting the sends of ONE step. Returns the counter dict; read
    ``["n"]`` for how many requests have gone out since. Bind it with
    :func:`step_bind` so calls made in copied contexts count too."""
    return {"n": 0}


def step_bind(counter: dict):
    try:
        return _STEP_CALLS.set(counter if isinstance(counter, dict) else None)
    except Exception:  # noqa: BLE001
        return None


def step_reset(token) -> None:
    if token is None:
        return
    try:
        _STEP_CALLS.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _unpack_token(token, session_id):
    """(ts, sid, epoch) from a record() token, with an explicit session_id
    winning. A malformed token is simply "no attribution", never an error."""
    ts = sid = epoch = None
    if isinstance(token, tuple) and len(token) == 3:
        ts, sid, epoch = token
    if session_id is not None:
        sid = _key(session_id)
    return ts, sid, epoch


def _bump_token_bucket_locked(ts: float, role, pt: int, ct: int) -> None:
    """Add this response's tokens to its MINUTE bucket. Caller holds ``_lock``.

    The per-role split is capped: model ids are operator-editable and mlx-lm's
    are filesystem paths, so an uncapped label set ships a six-figure blob to
    every polling browser.
    """
    key = _mkey(ts)
    b = _buckets.get(key)
    if b is None:
        b = _new_bucket()
        _buckets[key] = b
        while len(_buckets) > _BUCKETS + 1:
            _buckets.pop(min(_buckets), None)
    b["ti"] = int(b.get("ti") or 0) + pt
    b["to"] = int(b.get("to") or 0) + ct
    r = str(role or "").strip()[:_LABEL_MAX]
    if not (r and ct):
        return
    outs = b.setdefault("outs", {})
    if r in outs or len(outs) < _LABELS_PER_BUCKET:
        outs[r] = outs.get(r, 0) + ct
    else:
        outs[_OVERFLOW_BUCKET] = outs.get(_OVERFLOW_BUCKET, 0) + ct


def _bill_session_tokens_locked(sid, epoch, pt: int, ct: int) -> None:
    """Charge tokens to a chat session, and to its turn when the token was
    stamped with the turn still current. Caller holds ``_lock``."""
    slot = _slot(sid)
    slot["tokens_in"] = int(slot.get("tokens_in") or 0) + pt
    slot["tokens_out"] = int(slot.get("tokens_out") or 0) + ct
    if epoch is not None and epoch == slot["epoch"]:
        slot["turn_tokens_in"] = int(slot.get("turn_tokens_in") or 0) + pt
        slot["turn_tokens_out"] = int(slot.get("turn_tokens_out") or 0) + ct


def record_tokens(role: str | None = None, *, prompt_tokens: int = 0,
                  completion_tokens: int = 0, token=None, session_id=None,
                  now: float | None = None) -> None:
    """What the provider says this response actually cost, in tokens.

    Taken from the response body, never estimated: an estimate cannot tell you
    whether asking the model for shorter answers worked, which is the only
    reason to count this at all.

    ``token`` (from :func:`record`) attributes the tokens to the minute and
    turn of the SEND, exactly as failures are. Without one the tokens still
    count machine-wide — a response IS evidence a request happened, so unlike a
    failure there is no subset invariant to break — but they land on the
    current minute and on no turn. Never raises.
    """
    global _tokens_in_total, _tokens_out_total
    try:
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
        if not pt and not ct:
            return
        ts, sid, epoch = _unpack_token(token, session_id)
        # NO ambient-context fallback, for the reason record_failure documents:
        # re-reading the context at SETTLE time bills a background fold's
        # tokens to whatever chat the thread has been rebound to since. A
        # fold's 4000 written tokens landing on a chat that sent one request is
        # not a better guess than "unattributed" — it is a wrong one.
        with _lock:
            _now = time.monotonic() if now is None else now
            if not isinstance(ts, (int, float)) or ts > _now:
                ts = _now
            _tokens_in_total += pt
            _tokens_out_total += ct
            if _mkey(ts) >= _mkey(_now) - (_BUCKETS - 1):
                _bump_token_bucket_locked(ts, role, pt, ct)
            if sid is not None and sid in _sessions:
                _bill_session_tokens_locked(sid, epoch, pt, ct)
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
        slot["turn_failed"] = 0
        slot["turn_tokens_out"] = 0
        slot["turn_tokens_in"] = 0
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


def reset_all() -> None:
    """Test helper — drop every counter."""
    # `_dropped_at` was NOT in this global list (and `_dropped`, which was, does
    # not exist): the assignment below bound a local and left the real flag set,
    # so one test that overflowed the ring left every later `global_snapshot`
    # claiming `rate_capped` for the rest of the process.
    global _total, _fail_total, _started, _dropped_at
    global _tokens_in_total, _tokens_out_total
    with _lock:
        _total = 0
        _fail_total = 0
        _tokens_in_total = 0
        _tokens_out_total = 0
        _dropped_at = 0.0
        _started = time.monotonic()
        _recent.clear()
        _recent_fail.clear()
        _buckets.clear()
        _sessions.clear()


__all__ = ["record", "record_failure", "record_tokens", "turn_reset",
           "bind_turn", "reset_turn", "step_begin", "step_bind", "step_reset",
           "snapshot", "global_snapshot", "reset_all", "WINDOWS"]
