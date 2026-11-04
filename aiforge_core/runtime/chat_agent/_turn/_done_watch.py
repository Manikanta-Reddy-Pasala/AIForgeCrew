"""The harness sees the work is done, instead of waiting for the model to say so.

A turn ended only when the model replied without a tool call. On the big
benchmarks it never did: 50 minutes of runs against OpenCode's 17-40, the
board at "5 of 5 still open" while every named part was in the code. The
model kept probing, re-reading and re-testing work that was finished.

The work counts as done when, on the code as it is now:

* the full test suite passed (``st.last_green_fp``, set by a whole-suite run
  — a failure the request itself names, "`test_lazy_load` already fails",
  does not make it red) and no edit landed since; and
* every name, signature and file the request spells out is in the code
  (``spec_contract``) — or, for a request that names fewer than three, no
  board item is still untouched (pending).

When the run goes ``IDLE`` steps without an edit with every named part
in place, the harness runs the project's tests itself (once per code
state): the model's own runs are often piped or narrowed and prove nothing.

Then, once, the open board items are closed with that as their evidence and
the model is told to write its final answer. A model that still makes no
edit for ``GRACE`` more steps is finished by the harness: the turn ends with
a summary of what changed. An edit after the note means it was not done
after all — the watch waits for the next green suite. A message from the
user resets it. Long runs are untouched until their work checks out.
Off switch: ``AIFORGE_CHAT_DONE_WATCH=0``; grace: ``AIFORGE_CHAT_DONE_GRACE``.
"""
from __future__ import annotations

import os

_MIN_CLAIMS = 3


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_DONE_WATCH", "1").strip().lower() not in (
        "0", "false", "no", "off")


def grace() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_CHAT_DONE_GRACE", "6")))
    except ValueError:
        return 6


def _applies(st) -> bool:
    return not any(getattr(st, k, False) for k in (
        "readonly_mode", "plan_mode", "analyze_mode", "strict_finish", "builder"))


def _spec_complete(st) -> bool:
    """Every name the request spells out is in the code; a request that
    names fewer than three falls back to the board being closed."""
    from aiforge_core.runtime import spec_contract

    from ._tasks import open_items
    goal = str(getattr(st, "goal", "") or "")
    try:
        claims = spec_contract.claims(goal)
    except Exception:  # noqa: BLE001
        claims = []
    if len(claims) >= _MIN_CLAIMS:
        from ._contract_gate import _gaps, _waived
        return not _waived(st, _gaps(st))
    # Few names to check: no item may be untouched. A running one may well be
    # finished — the board staying open is the very thing that kept runs
    # going; the note lets the model name and do what is really missing.
    board = getattr(st, "board", None) or {}
    return not any(board[s].get("status") == "pending" for s in open_items(board))


def _green_now(st) -> bool:
    """The full suite passed on the code as it is now."""
    if not getattr(st, "last_green_fp", None):
        return False
    if getattr(st, "edits_made", 0) != getattr(st, "green_at_edits", -1):
        return False
    try:
        from ._outcomes import content_fingerprint
        return content_fingerprint(getattr(st, "cwd", None)) == st.last_green_fp
    except Exception:  # noqa: BLE001
        return False


#: Steps with no edit, every named part present, before the harness runs
#: the tests itself (the model's own runs are often piped or narrowed).
IDLE = 8


def _idle_steps(st, n) -> int:
    edits = getattr(st, "edits_made", 0)
    if getattr(st, "dw_edits", None) != edits:
        st.dw_edits, st.dw_edit_step = edits, n
    return n - getattr(st, "dw_edit_step", n)


def _probe_tests(st, n):
    """Generator: run the project's tests once for this code state when the
    run has gone ``IDLE`` steps without an edit and every named part is in.
    Sets the green fingerprint when they pass (a failure the request names
    does not count)."""
    if _green_now(st) or _idle_steps(st, n) < IDLE:
        return
    edits = getattr(st, "edits_made", 0)
    if getattr(st, "dw_probed_at", None) == edits or not _spec_complete(st):
        return
    st.dw_probed_at = edits
    yield {"type": "thought", "role": "system",
           "text": "⧗ no edits for a while and every named part is in — running "
                   "the project's tests to see whether the work is done"}
    from .._context import _run_project_verify
    from ._outcomes import _only_known_failures, content_fingerprint
    ok, out = _run_project_verify(getattr(st, "cwd", None))
    if ok is True or (ok is False and _only_known_failures(st, {"ok": False, "stdout": out})):
        st.last_green_fp = content_fingerprint(getattr(st, "cwd", None))
        st.green_at_edits = edits


def done_now(st) -> bool:
    if not _on() or not _applies(st) or getattr(st, "edits_made", 0) <= 0:
        return False
    try:
        return _green_now(st) and _spec_complete(st)
    except Exception:  # noqa: BLE001 — the watch never ends a turn on its own error
        return False


def _close_board(st) -> list:
    """Open board items closed with the harness's evidence; UI events."""
    from ._tasks import board_items, open_items
    board = getattr(st, "board", None) or {}
    left = open_items(board)
    for slug in left:
        board[slug]["status"] = "done"
        board[slug]["note"] = ("done (harness): the full test suite passes and every "
                               "part the request names is in the code")
    return [{"type": "subtasks", "items": board_items(board)}] if left else []


def _summary(st) -> str:
    try:
        from aiforge_core.runtime import action_log
        rows = action_log.changed_files(action_log.entries(
            action_log.session_steps(getattr(st, "session_id", None))))
    except Exception:  # noqa: BLE001
        rows = []
    lines = [f"- {p}" + (f" ({', '.join(n[:6])})" if n else "") for p, _, n in rows[-20:]]
    return ("Done. The full test suite passes on the final code and every part the "
            "request names is in place.\n\n"
            + ("Files changed:\n" + "\n".join(lines) + "\n\n" if lines else "")
            + "(The harness ended the turn here: the work checked out and the "
              "following steps changed nothing.)")


def watch(st, n):
    """Generator, once per step after steering is drained: the note when the
    work is first seen done, the end of the turn when the model keeps going
    without changing anything. Returns "return" to end the turn."""
    if getattr(st, "steers_since_done", None) is not None and len(
            getattr(st, "steers", None) or []) != st.steers_since_done:
        st.done_noted_at = None          # the user spoke: their words decide
    if _on() and _applies(st) and getattr(st, "edits_made", 0) > 0:
        try:
            yield from _probe_tests(st, n)
        except Exception:  # noqa: BLE001 — the probe never ends a turn
            pass
    if not done_now(st):
        if getattr(st, "done_noted_at", None) is not None and \
                getattr(st, "edits_made", 0) != getattr(st, "done_edits", -1):
            st.done_noted_at = None      # it edited: not done after all
        return None
    if getattr(st, "done_noted_at", None) is None:
        st.done_noted_at = n
        st.done_edits = getattr(st, "edits_made", 0)
        st.steers_since_done = len(getattr(st, "steers", None) or [])
        for ev in _close_board(st):
            yield ev
        yield {"type": "thought", "role": "system",
               "text": "✓ the work checks out (full suite green, every named part "
                       "present) — asking for the final answer"}
        from ._convo import _append_directive
        _append_directive(st, (
            "[harness — not the user] Done check: the full test suite passes on the "
            "current code and every name, signature and file the request spells out "
            "is in the code; the task board is closed. Unless something the request "
            "asks for is still missing (then name it and do it), stop now: reply "
            "with your final summary and no tool call."))
        return None
    if n - st.done_noted_at < grace():
        return None
    yield {"type": "thought", "role": "system",
           "text": f"✓ {grace()} steps after the done check and nothing changed — "
                   "ending the turn"}
    yield {"type": "message", "text": _summary(st)}
    yield {"type": "done"}
    return "return"


__all__ = ["done_now", "grace", "watch"]
