"""A task-board item that has eaten far more than its share of the time, with
other parts of the request not started, is told once to note what still
fails and move on.

shopkit v6a: parts 1-2 done in 35 minutes, then 16 minutes on one failing
part-3 test until the rig's clock ran out; parts 4 and 5 (24 of 60 hidden
tests) were never started. click t4: the same with completion order while
``_fast_exit`` was never written. A part left half-working costs a few tests;
a part never started costs all of them.

The clock is wall time between steps, charged to the item being worked (the
one most recently set ``running``). Limit: ``AIFORGE_CHAT_ITEM_TIMEBOX_S`` (900 s) for the
first item; once items have closed, twice their mean (at least 600 s, or the
setting when it is lower). In the
last time slice of a turn with a deadline, at most 1.5x a fair share of the
time left. Off with ``AIFORGE_CHAT_ITEM_TIMEBOX_S=0``. Only on a board the
item loop runs (``_items.active``). The item goes back to pending (never
closed): the board's FINAL reminder brings it back once the others are done.
Wall time includes waits between steps (an approval, a model stall): on an
unattended run there are none worth the bookkeeping.
"""
from __future__ import annotations

import os
import time

from ._tasks import _CLOSED, open_items

_FLOOR_S = 600
_LAST_SLICE_FLOOR_S = 180
_STEP_CAP_S = 300


def _first_limit() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_CHAT_ITEM_TIMEBOX_S", "900")))
    except ValueError:
        return 900.0


def _current(st):
    """The item being worked: the one most recently set running (an item
    left running when the model moved on is not charged any more)."""
    run = [s for s in open_items(st.board) if st.board[s].get("status") == "running"]
    fresh = [s for s in run if s not in st.item_running]
    st.item_running = set(run)
    if fresh:
        st.item_cur = fresh[0]
    elif st.item_cur not in run:
        st.item_cur = run[0] if run else None
    return st.item_cur


def _limit(st, cur: str, now: float) -> float:
    clock = st.item_clock
    closed = [clock.get(s, 0.0) for s, it in st.board.items()
              if it.get("status") in _CLOSED and clock.get(s, 0.0) > 0]
    first = _first_limit()
    limit = max(min(_FLOOR_S, first), 2 * sum(closed) / len(closed)) if closed else first
    deadline = getattr(st, "turn_deadline", None)
    if deadline is not None and st.extensions_used >= st.ext_budget:
        left = max(0.0, deadline - now)
        share = (left + clock.get(cur, 0.0)) / max(1, len(open_items(st.board)))
        limit = min(limit, max(_LAST_SLICE_FLOOR_S, 1.5 * share))
    return limit


def check(st, now: "float | None" = None) -> "str | None":
    """Charge the time since the last step to the item being worked; return
    the move-on note when it is over its limit (once per item)."""
    if not _first_limit():
        return None
    from ._items import active
    if not active(st):
        return None
    now = time.monotonic() if now is None else now
    if not hasattr(st, "item_clock") or st.item_clock is None:
        st.item_clock, st.item_clock_at, st.item_boxed = {}, now, set()
        st.item_running, st.item_cur = set(), None
    # the step that just ran was worked on the item current when it began
    # (the step that closes an item and starts the next is the old one's)
    was = st.item_cur
    if was is not None:
        # one step's charge is capped: an approval wait or a model outage
        # between two steps is not work on the item
        step = min(max(0.0, now - st.item_clock_at), _STEP_CAP_S)
        st.item_clock[was] = st.item_clock.get(was, 0.0) + step
    st.item_clock_at = now
    cur = _current(st)
    if cur is None or cur in st.item_boxed:
        return None
    deadline = getattr(st, "turn_deadline", None)
    if getattr(st, "time_warned", False) or getattr(st, "deadline_final", None) is not None \
            or (deadline is not None and deadline - now <= 0):
        return None             # the wrap-up / final-answer note owns the step
    # parts not started: never one the model added itself, never one it was
    # already told to leave
    waiting = [s for s in open_items(st.board)
               if s != cur and st.board[s].get("status") == "pending"
               and not st.board[s].get("self_added") and s not in st.item_boxed]
    spent = st.item_clock.get(cur, 0.0)
    if not waiting or spent <= _limit(st, cur, now):
        return None
    st.item_boxed.add(cur)
    nxt = waiting[0]
    return ("[harness — not the user] Task-board item "
            f"{cur} ({st.board[cur]['title']}) has taken {int(spent // 60)} min, "
            f"and {len(waiting)} item(s) are not started yet. A part never "
            "started fails every test of it; a part left half-working fails a "
            f"few. Set {cur} back to pending with plan_progress (say in one line "
            f"what still fails in it), mark {nxt}: {st.board[nxt]['title']} "
            "running and do it now (with its own test run), and come back to "
            f"{cur} when the others are done.")


__all__ = ["check"]
