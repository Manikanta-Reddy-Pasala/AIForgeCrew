"""Batched reads: running a native reply's extra read-only calls without
asking the model again."""
from __future__ import annotations

import contextvars
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from .._context import (
    _tail_budget_chars,
    _text_of,
)
from ._convo import (
    _append_directive,
)
from ._tool_dispatch import _invoke_tool


def _parallel_cap() -> int:
    """Most slow reads of one batch running in the background at once,
    besides the reply's first call (servers rate-limit)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_PARALLEL_READS", "4")))
    except (TypeError, ValueError):
        return 4


def _call_sig(name, args):
    """One call's identity: the loop matches a started read by it."""
    return name + "|" + json.dumps(args, sort_keys=True, default=str)


def _queue_batched_reads(st, n):
    """Keep the read-only calls the model batched after its first one. Called
    right after model call ``n``, so any earlier batch has now been read."""
    take = getattr(st.complete_fn, "take_queued", None)
    if not callable(take):
        return
    steps, skipped = take()
    st.pending_steps[:] = steps
    st.batch_skipped = skipped
    st.batch_mark = len(st.convo)
    st.batch_unread = bool(steps)
    _cancel_early_reads(st)
    # The batch runs from step n + 1: a batch that would stop there starts nothing.
    if steps and _batch_stop_reason(st, n + 1, st.session_id) is None:
        try:
            _start_parallel_reads(st)
        except Exception:  # noqa: BLE001 — e.g. no thread left: run them in line
            _cancel_early_reads(st)


def _can_start_early(st, name, args, sig):
    """A read may run ahead of its turn only when no gate could stop it: the
    policy allows it outright, no PreToolUse hook watches it, the repeat
    guard will not skip it as already read, and a page fetch passes the egress
    gate (tool_policy does not look at URLs). Anything else waits and passes
    the gates in order."""
    from aiforge_core.net import egress
    from aiforge_core.runtime import hooks
    from aiforge_core.runtime.tools import tool_policy

    from .._native import CONCURRENT_READS
    from .._registry import TOOLS
    if name not in CONCURRENT_READS or name not in TOOLS:
        return False
    if st.long_chain_help and sig in st.read_sigs_seen:
        return False
    if name == "web_fetch" and egress.check(str(args.get("url") or "")):
        return False
    try:
        if tool_policy.decide(name, args)["policy"] != tool_policy.ALLOW:
            return False
    except Exception:  # noqa: BLE001 — unsure: let the gates decide
        return False
    return not hooks.has_matching("PreToolUse", name, st.cwd)


def _start_parallel_reads(st):
    """Start the batch's slow reads now, a few at a time and alongside the
    reply's first call, so the loop finds each result ready when it gets there.
    The loop still walks the batch in order through every gate; the reads of a
    dropped batch are cancelled or go unused."""
    from .._prompt import _parse
    from .._registry import TOOLS
    calls = {}
    for text in st.pending_steps:
        step = _parse(text)
        if step.get("kind") != "action":
            continue
        name = step["tool"]
        args = step["args"] if isinstance(step["args"], dict) else {}
        sig = _call_sig(name, args)
        if sig not in calls and _can_start_early(st, name, args, sig):
            calls[sig] = (name, args)
    cap = _parallel_cap()
    if not calls or cap == 0:
        return
    pool = ThreadPoolExecutor(max_workers=min(cap, len(calls)),
                              thread_name_prefix="batch-read")
    for sig, (name, args) in calls.items():
        # The tool gets its own copy of args (the loop keeps the model's), and
        # the caller's context vars, which a thread does not inherit.
        st.early_reads[sig] = pool.submit(
            contextvars.copy_context().run, _invoke_tool,
            TOOLS[name], name, json.loads(json.dumps(args, default=str)), st.cwd)
    pool.shutdown(wait=False)


def _cancel_early_reads(st):
    """Forget the started reads; the ones still waiting for a worker never run."""
    for future in st.early_reads.values():
        future.cancel()
    st.early_reads = {}


def _take_early_read(st, sig):
    """The started read for this call, or None to run it now."""
    return st.early_reads.pop(sig, None)


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
    _cancel_early_reads(st)
    if parts and reason != "stopped":
        _append_directive(st, (
            "[batch note — not the user] Some tool calls in your last reply "
            "did not run: "
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
