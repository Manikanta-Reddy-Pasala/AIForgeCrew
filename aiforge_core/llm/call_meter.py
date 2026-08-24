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


def _key(session_id) -> "str | None":
    return None if session_id in (None, "") else str(session_id)


def _slot(sid: str) -> dict:
    slot = _sessions.get(sid)
    if slot is None:
        slot = {"total": 0, "turn": 0, "by_role": {}, "epoch": 0,
                "failed": 0, "turn_failed": 0,
                "tokens_out": 0, "turn_tokens_out": 0,
                "tokens_in": 0, "turn_tokens_in": 0}
        _sessions[sid] = slot
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)     # oldest out
    else:
        _sessions.move_to_end(sid)
    return slot


def _attribute(sid, role):
    """(session, role) for this call, falling back to the request context.

    The wire-level caller (_post) knows neither — both ride the request
    context, which the chat turn binds and the generation thread inherits.

    CONTEXTVAR ONLY. request_context.get_session_id() also falls back to the
    process-global AIFORGE_CURRENT_SESSION env var, which the chat route sets
    and never clears — so every bare thread in the system (session folds,
    learners, classifiers) would bill its calls to whichever chat last ran a
    turn. An unattributed call is correct; a call billed to an innocent chat
    is not.
    """
    if sid is not None and role:
        return sid, role
    try:
        from aiforge_core.runtime import request_context
        if sid is None:
            sid = _key(request_context.context_session_id())
        role = role or request_context.get_role()
    except Exception:  # noqa: BLE001
        pass
    return sid, role


def _current_epoch():
    try:
        return _TURN_EPOCH.get()
    except Exception:  # noqa: BLE001
        return None


def _bump_step_counter() -> None:
    """One more call inside the current ReAct step, when a step is bound."""
    try:
        _step = _STEP_CALLS.get()
        if isinstance(_step, dict):
            _step["n"] = int(_step.get("n") or 0) + 1
    except Exception:  # noqa: BLE001
        pass


def _bill_session_locked(sid, role, epoch):
    """Charge one call to a chat session. Returns the epoch to STAMP the token
    with. Caller holds ``_lock``.

    A cancelled generation is ABANDONED, not stopped: its thread keeps retrying
    with the turn's context still bound. Those calls belong to the turn that
    made them, not to whatever the user typed next — so the per-turn counter
    only accepts calls stamped with the CURRENT turn's epoch (None = a caller
    outside any turn, e.g. a background fold).
    """
    slot = _slot(sid)
    slot["total"] += 1
    if epoch is None or epoch == slot["epoch"]:
        slot["turn"] += 1
        # Stamp the token with the turn this call was COUNTED against.
        # Carrying the caller's bare None instead meant a failure resolved
        # `epoch is None` at settle time and landed on whatever turn was
        # current THEN — the exact thing the token exists to prevent, and
        # visible as "0 requests · 1 failed" on a message that sent nothing.
        epoch = slot["epoch"]
    if role:
        slot["by_role"][role] = slot["by_role"].get(role, 0) + 1
    return epoch


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


def record_failure(token=None, reason: str | None = None, *,
                   session_id=None, now: float | None = None) -> None:
    """One request that :func:`record` counted did NOT come back with an
    answer. Never raises.

    ``token`` is what ``record`` returned for that very request, and it is
    REQUIRED: without one this is a no-op. That is what makes "failures are a
    subset of requests" structural rather than a promise — ``record`` returns
    None only when it could not count the send, and counting a failure for an
    uncounted send puts ``failed`` above ``total``, paints a sparkline minute
    with nothing to scale it against, and lets a broken meter report traffic
    the box never sent. The token also carries the minute, chat and turn of
    the SEND, which is what the failure is billed to.

    ``reason`` is a short label (``http_500``, ``timeout``, ``cancelled``,
    ``empty``…). It is clipped and the per-minute label set is capped, because
    an unbounded reason (a stringified exception) would ship an exception novel
    to every polling browser.
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
        # attribute a fold's timeout to whatever chat the thread has been
        # rebound to since — a different chat, not a better guess.
        with _lock:
            _now = time.monotonic() if now is None else now
            if not isinstance(ts, (int, float)) or ts > _now:
                # No usable stamp, or one from the future (a clock seam, or a
                # hand-fed `now` in a test): treat it as happening now.
                ts = _now
            _fail_total += 1
            # A send older than the reported history has NO minute left to be
            # charged to. It is counted in the lifetime total (it happened) and
            # left out of every window — the send it belongs to is outside
            # those windows too. Re-stamping it to the current minute instead
            # would report a burst that never happened AND put `failed_60m`
            # above `last_60m`: the meter's one invariant, and the reason the
            # token is mandatory, broken at window level. The windows read 59
            # whole minutes back (`_bucket_sum_locked`), not `_RETAIN_S`, so
            # the cutoff has to match them or a 59-to-60-minute-old failure
            # lands in a bucket nothing reports.
            if _mkey(ts) < _mkey(_now) - (_BUCKETS - 1):
                return
            if _now - ts < _WINDOW_S:
                # Only stamps inside the exact window matter to the rate; the
                # rest would be trimmed on the next read anyway, and keeping
                # them out bounds the insort.
                bisect.insort(_recent_fail, ts)
                del _recent_fail[:max(0, len(_recent_fail) - _RECENT_MAX)]
            _bump_fail_bucket_locked(ts, reason)
            if sid is not None and sid in _sessions:
                # Only a session the meter already knows: a failure must not
                # mint a slot (and evict a live one) for a chat that never sent
                # anything through this process.
                slot = _slot(sid)
                slot["failed"] = int(slot.get("failed") or 0) + 1
                # A REAL epoch match only. `record` stamps the token with the
                # turn it counted the send against, so an unstamped failure is
                # one whose turn is unknown — and guessing "the current one"
                # can put `turn_failed` above a `turn` that never included the
                # send. Undercounting one turn beats billing an innocent one.
                if epoch is not None and epoch == slot["epoch"]:
                    slot["turn_failed"] = int(slot.get("turn_failed") or 0) + 1
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
        outs["…other"] = outs.get("…other", 0) + ct


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


def _trim_recent_locked(now: float) -> None:
    """Keep the raw ring to the last 60 seconds. Destructive, cheap, and the
    reason the per-minute rate stays exact and O(1)-ish to read."""
    cutoff = now - _WINDOW_S
    while _recent and _recent[0] < cutoff:
        _recent.popleft()
    # Same window, sorted list (see `_recent_fail`): drop the aged-out head.
    i = bisect.bisect_left(_recent_fail, cutoff)
    if i:
        del _recent_fail[:i]


def _mkey(ts: float) -> int:
    return int(ts // _MINUTE_S)


def _new_bucket() -> dict:
    # `ti`/`to` = tokens in / out for the minute; `outs` = out-tokens by role,
    # which is the breakdown that names WHICH agent is writing an essay.
    return {"n": 0, "f": 0, "ti": 0, "to": 0, "roles": {}, "provs": {},
            "models": {}, "fails": {}, "outs": {}}


def _bump_bucket_locked(ts: float, role, provider, model) -> None:
    key = _mkey(ts)
    b = _buckets.get(key)
    if b is None:
        b = _new_bucket()
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


def _bump_fail_bucket_locked(ts: float, reason) -> None:
    """Charge one failure to the minute of its SEND. Creates the bucket if the
    minute has no successful send in it (a minute in which every attempt failed
    is the most important minute the meter can show)."""
    key = _mkey(ts)
    b = _buckets.get(key)
    if b is None:
        b = _new_bucket()
        _buckets[key] = b
        while len(_buckets) > _BUCKETS + 1:
            _buckets.pop(min(_buckets), None)
    b["f"] = int(b.get("f") or 0) + 1
    v = str(reason or "").strip()[:_LABEL_MAX] or "error"
    fails = b.setdefault("fails", {})
    if v in fails or len(fails) < _LABELS_PER_BUCKET:
        fails[v] = fails.get(v, 0) + 1
    else:
        fails["…other"] = fails.get("…other", 0) + 1


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


def _fail_sum_locked(now: float, minutes: int) -> int:
    """Failures over the same window as :func:`_bucket_sum_locked`."""
    first = _mkey(now) - (minutes - 1)
    return sum(int(b.get("f") or 0) for k, b in _buckets.items() if k >= first)


def _token_sums_locked(now: float, minutes: int) -> "tuple[int, int]":
    first = _mkey(now) - (minutes - 1)
    ti = to = 0
    for k, b in _buckets.items():
        if k < first:
            continue
        ti += int(b.get("ti") or 0)
        to += int(b.get("to") or 0)
    return ti, to


def _tokens_by_role_locked(now: float, minutes: int) -> dict:
    first = _mkey(now) - (minutes - 1)
    out: dict = {}
    for k, b in _buckets.items():
        if k < first:
            continue
        for name, n in (b.get("outs") or {}).items():
            out[name] = out.get(name, 0) + n
    return out


def _fail_reasons_locked(now: float, minutes: int) -> dict:
    first = _mkey(now) - (minutes - 1)
    out: dict = {}
    for k, b in _buckets.items():
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
    for k, b in _buckets.items():
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
    newest = _mkey(now)
    first = newest - (_BUCKETS - 1)
    return [int((_buckets.get(k) or {}).get(field) or 0)
            for k in range(first, newest + 1)]


def _per_minute_locked(now: float) -> int:
    _trim_recent_locked(now)
    return len(_recent)


def _fail_per_minute_locked(now: float) -> int:
    _trim_recent_locked(now)      # trims both rings
    return len(_recent_fail)


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
            # Failures are a SUBSET of the counts above, never a separate
            # population: `turn` is every attempt this message made and
            # `turn_failed` is how many of them came back with nothing.
            "turn_failed": int((slot or {}).get("turn_failed") or 0),
            "session_failed": int((slot or {}).get("failed") or 0),
            "failed": _fail_total,
            "failed_per_minute": _fail_per_minute_locked(now),
            # What the model actually WROTE for this message and this chat —
            # the number a "be brief" instruction is meant to move, and the
            # one the request count cannot show (40 one-line steps and one
            # 6000-token essay are both "41 requests").
            "turn_tokens_out": int((slot or {}).get("turn_tokens_out") or 0),
            "session_tokens_out": int((slot or {}).get("tokens_out") or 0),
            "turn_tokens_in": int((slot or {}).get("turn_tokens_in") or 0),
            "session_tokens_in": int((slot or {}).get("tokens_in") or 0),
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
    _limits = _limit_state()
    with _lock:
        now = time.monotonic()
        _prune_buckets_locked(now)
        by_role, by_provider, by_model = _breakdown_locked(now, _BUCKETS)
        out = {
            "total": _total,
            "per_minute": _per_minute_locked(now),
            "last_15m": _bucket_sum_locked(now, 15),
            "last_60m": _bucket_sum_locked(now, _BUCKETS),
            # How many of those attempts failed, over the SAME windows — a
            # subset of the numbers above, not a second population. A rate that
            # hid them would read lowest exactly when the box is in trouble.
            "failed": _fail_total,
            "failed_per_minute": _fail_per_minute_locked(now),
            "failed_15m": _fail_sum_locked(now, 15),
            "failed_60m": _fail_sum_locked(now, _BUCKETS),
            "by_fail_reason": _fail_reasons_locked(now, _BUCKETS),
            # Tokens as REPORTED by the provider, over the same windows.
            "tokens_in": _tokens_in_total,
            "tokens_out": _tokens_out_total,
            "tokens_out_15m": _token_sums_locked(now, 15)[1],
            "tokens_out_60m": _token_sums_locked(now, _BUCKETS)[1],
            "tokens_in_60m": _token_sums_locked(now, _BUCKETS)[0],
            "tokens_out_by_role": _tokens_by_role_locked(now, _BUCKETS),
            "by_role": by_role,
            "by_provider": by_provider,
            "by_model": by_model,
            "uptime_s": round(now - _started, 1),
            # The operator's ceiling and how many callers are parked on it.
            # Without these a throttled box looks broken rather than capped —
            # the meter is where someone goes to ask "why is this slow".
            **_limits,
            # An ACTUAL loss, and only within the window it can affect: the
            # 60s ring evicted calls that would otherwise be in `per_minute`.
            "rate_capped": (now - _dropped_at) < _WINDOW_S if _dropped_at else False,
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
