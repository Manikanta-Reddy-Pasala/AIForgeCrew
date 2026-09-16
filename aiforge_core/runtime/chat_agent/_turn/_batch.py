"""Batched reads: running a native reply's extra read-only calls without
asking the model again."""
from __future__ import annotations

import time

from .._context import (
    _tail_budget_chars,
    _text_of,
)
from ._convo import (
    _append_directive,
)


def _queue_batched_reads(st):
    """Keep the read-only calls the model batched after its first one. Called
    right after a model call, so any earlier batch has now been read."""
    take = getattr(st.complete_fn, "take_queued", None)
    if not callable(take):
        return
    steps, skipped = take()
    st.pending_steps[:] = steps
    st.batch_skipped = skipped
    st.batch_mark = len(st.convo)
    st.batch_unread = bool(steps)


def _unread_batch_msgs(st):
    """Messages a condense must keep: a batch's results the model has not read."""
    return len(st.convo) - st.batch_mark if st.batch_unread else 0


def _rebase_batch(st, unread):
    """Point batch_mark at the same results after a condense shortened the
    history (a condense keeps them, or gives up on them if they don't fit)."""
    if st.batch_unread:
        st.batch_mark = max(1, len(st.convo) - unread)


def _batch_stop_reason(st, n, session_id):
    """Why the rest of a batch must wait for the model, or None."""
    from aiforge_core.runtime import chat_cancel, chat_interject
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        return "stopped"
    if session_id is not None and chat_interject.pending(session_id):
        return "the user sent new instructions"
    # Leave the model a step to answer in: a capped run (Quick mode) must not
    # spend its whole budget on one batch.
    if st.capped and n >= st.safety:
        return "the step budget is nearly used up"
    if st.turn_deadline is not None and time.monotonic() > st.turn_deadline:
        return "the turn deadline passed"
    # Unread results are never condensed away, so a batch bigger than the tail
    # a condense keeps would crowd out the rest of the history.
    tail = _tail_budget_chars(st.convo, st.role)
    added = sum(len(_text_of(m)) for m in st.convo[st.batch_mark:])
    if tail and added > tail:
        return "their results would not fit in the context window"
    return None


_NOT_BATCHABLE = ("only quick read-only calls run together (at most "
                  "AIFORGE_CHAT_BATCH_READS per reply), and a call with "
                  "unreadable arguments never runs")


def _drop_batch(st, reason):
    """Tell the model which of its batched calls did not run, and why, once."""
    parts = []
    if st.pending_steps:
        parts.append(f"{len(st.pending_steps)} because {reason}")
    if st.batch_skipped:
        parts.append(f"{st.batch_skipped} because {_NOT_BATCHABLE}")
    st.pending_steps.clear()
    st.batch_skipped = 0
    if parts and reason != "stopped":
        _append_directive(st, (
            "NOTE: some tool calls in your last reply did not run: "
            + "; ".join(parts) + ". Request any you still need in your next reply."))


def _pop_queued_step(st, n, session_id):
    """The next batched read, or None: none left, or the run was stopped,
    steered, or hit a limit — then the model decides again."""
    if not st.pending_steps:
        if st.batch_skipped:
            _drop_batch(st, _NOT_BATCHABLE)
        return None
    reason = _batch_stop_reason(st, n, session_id)
    if reason:
        _drop_batch(st, reason)
        return None
    return st.pending_steps.pop(0)
