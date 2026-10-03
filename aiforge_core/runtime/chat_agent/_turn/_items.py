"""One task-board item at a time, each in a fresh context.

A big request is decomposed onto the task board (``_tasks``); this module is
what makes the board an execution plan instead of a checklist:

* **Evidence.** Every tool call is logged as it lands: files that really
  changed (content hashes, not the model's say-so), test runs and their runner
  summary, and green build/lint/check commands. An item marked ``done`` is
  accepted only with evidence in its window. Without it the item goes back to
  ``running`` with a different-approach hint (bounded by
  ``AIFORGE_CHAT_ITEM_RETRIES``); out of retries, an item whose last test run
  is red is ``failed`` and an item that simply left no trace is accepted but
  labelled unverified. Never silently "done".
* **Result note.** An accepted item gets a note written from that evidence
  (files changed, test summary, checks run), never from the model's prose.
  The note lives on the board and is pinned into the system message.
* **Context reset.** Once an item closes and others remain, the next step
  starts from a fresh context: system prompt + pinned original task + the
  board with every result note + one message naming the next item. The
  transcript of the finished work is saved (``context_offload``) and can be
  restored with ``memory_lookup``.

Only a run whose board has ``AIFORGE_CHAT_ITEM_MIN`` (3) or more items is
touched; a small request keeps the plain loop and pays nothing.
"""
from __future__ import annotations

import os
import re

from ._tasks import _CLOSED, open_items

_NOTE_MAX = 320
_PATHS_SHOWN = 8
_GENERIC_HINTS = (
    "Run the project's tests or build for this item, fix what fails, and mark "
    "it done only after they pass.",
    "Take a DIFFERENT approach this time: re-read the code this item touches, "
    "make the smallest change that satisfies the item, then run the check "
    "again. If the item cannot be done, mark it failed with plan_progress "
    "instead of repeating yourself.",
)
_CHECK_CMD = re.compile(
    r"\b(build|lint|compile|typecheck|mypy|ruff|tsc|flake8|eslint|cargo check|"
    r"go vet|py_compile)\b")
_TEST_LINE = re.compile(
    r"^.*\b\d+ (?:passed|failed|errors?)\b.*$|^\s*Tests:\s.*$|^.*BUILD "
    r"(?:SUCCESS|FAILURE).*$|^.*test result:.*$", re.M)


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "0", "false", "off", "no", "")


def reset_enabled() -> bool:
    """Context reset between board items (``AIFORGE_CHAT_ITEM_CONTEXT_RESET``)."""
    return _flag("AIFORGE_CHAT_ITEM_CONTEXT_RESET")


def verify_enabled() -> bool:
    """Evidence check before an item counts as done (``AIFORGE_CHAT_ITEM_VERIFY``)."""
    return _flag("AIFORGE_CHAT_ITEM_VERIFY")


def min_items() -> int:
    try:
        return max(2, int(os.environ.get("AIFORGE_CHAT_ITEM_MIN", "3")))
    except ValueError:
        return 3


def max_retries() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_ITEM_RETRIES", "2")))
    except ValueError:
        return 2


def item_fields() -> dict:
    """The loop-state fields this module keeps."""
    return {"item_log": None, "item_seen": {}, "item_start": 0,
            "item_prev_start": None, "item_rejects": {},
            "item_reset_pending": False, "item_resets": 0,
            "item_last_closed": None, "item_fresh_close": False}


def active(st) -> bool:
    """A board big enough to be run item by item, on a run that edits."""
    board = getattr(st, "board", None) or {}
    return (len(board) >= min_items() and not getattr(st, "readonly_mode", False)
            and not getattr(st, "builder", None)
            and (reset_enabled() or verify_enabled()))


def _merged(st) -> dict:
    return {**getattr(st, "file_hashes", {}), **getattr(st, "tree_hashes", {})}


def ensure_started(st) -> None:
    """Begin the evidence log, once the board is big enough. What the tree
    already held (the user's own dirty files) is the baseline, not a change."""
    if getattr(st, "item_log", None) is not None or not active(st):
        return
    seen = _merged(st)
    try:
        from ._progress import _tracked_changes
        base = _tracked_changes(st, getattr(st, "cwd", None))
        if base:
            seen = {**base, **seen}
    except Exception:  # noqa: BLE001 — no baseline: only tool-written files count
        pass
    st.item_seen = dict(seen)
    st.item_log = []


def _sync_edits(st) -> None:
    now = _merged(st)
    changed = [p for p, h in now.items() if st.item_seen.get(p) != h]
    if changed:
        st.item_seen.update({p: now[p] for p in changed})
        st.item_log.append(("edit", changed))


def _test_summary(result) -> str:
    from aiforge_core.runtime.failure_signature import result_text
    text = result_text(result)
    hits = _TEST_LINE.findall(text)
    line = hits[-1] if hits else ""
    return " ".join(line.split())[:120]


def note_evidence(st, name, args, result) -> None:
    """Log what one finished tool call proved. Called after every tool call;
    does nothing until a big board started the log."""
    if getattr(st, "item_log", None) is None or name == "plan_progress":
        return
    try:
        _sync_edits(st)
        from aiforge_core.runtime import cmd_finished

        from ._outcomes import _is_test_run
        run = cmd_finished.as_run(name, args, result)
        if run is None or not isinstance(run[2], dict):
            return
        rname, rargs, rres = run
        ok = rres.get("ok")
        if _is_test_run(rname, rargs):
            from aiforge_core.runtime.failure_signature import (
                result_text,
                runner_green,
            )
            green = ok is True and runner_green(result_text(rres))
            st.item_log.append(("test", "green" if green else "red",
                                _test_summary(rres)))
        elif ok is True:
            cmd = str((rargs or {}).get("cmd") or (rargs or {}).get("command") or "")
            if cmd and _CHECK_CMD.search(cmd):
                st.item_log.append(("check", " ".join(cmd.split())[:80]))
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a turn
        pass


def _window(st):
    """Evidence events of the item being closed. An item closed right after
    another, with no tool call between (one reply marking several done), shares
    the evidence of the one before it."""
    log = st.item_log
    evs = log[st.item_start:]
    if (not evs and st.item_prev_start is not None
            and getattr(st, "item_fresh_close", False)):
        return log[st.item_prev_start:], True
    return evs, False


def _rel(path, cwd) -> str:
    try:
        return os.path.relpath(path, cwd) if cwd else path
    except ValueError:
        return path


def _facts(st, evs) -> dict:
    edited: list[str] = []
    for ev in evs:
        if ev[0] == "edit":
            edited += [_rel(p, getattr(st, "cwd", None)) for p in ev[1]]
    edited = list(dict.fromkeys(edited))
    tests = [e for e in evs if e[0] == "test"]
    checks = list(dict.fromkeys(e[1] for e in evs if e[0] == "check"))
    return {"edited": edited, "test": tests[-1] if tests else None,
            "checks": checks}


def _note(facts: dict, status: str, suffix: str = "") -> str:
    bits = []
    if facts["edited"]:
        shown = facts["edited"][:_PATHS_SHOWN]
        more = len(facts["edited"]) - len(shown)
        bits.append("files: " + ", ".join(shown) + (f" (+{more} more)" if more else ""))
    if facts["test"]:
        _, state, line = facts["test"]
        bits.append(f"tests {state}" + (f" ({line})" if line else ""))
    if facts["checks"]:
        bits.append("checks ok: " + "; ".join(facts["checks"][:3]))
    if not bits:
        bits.append("no file changes or checks recorded")
    head = {"done": "done", "failed": "FAILED", "skipped": "skipped"}.get(
        status, status)
    return (head + ": " + " | ".join(bits) + suffix)[:_NOTE_MAX]


def _verdict(facts: dict):
    """``(ok, evidence_or_reason)``."""
    test = facts["test"]
    if test and test[1] == "red":
        return False, ("the last test run for this item FAILED"
                       + (f" ({test[2]})" if test[2] else ""))
    if test:
        return True, "tests green"
    if facts["edited"]:
        return True, "files changed"
    if facts["checks"]:
        return True, "check green"
    return False, ("nothing shows this item was done: no file changed and no "
                   "test or build ran since the previous item")


def review_progress(st, result: dict, events: list):
    """Judge a ``plan_progress`` call that closed a board item. Returns the
    ``(result, events)`` the model and UI should see; may put the item back to
    ``running`` (not accepted) and arms the context reset when it closes."""
    try:
        ensure_started(st)
        slug = result.get("slug")
        item = st.board.get(slug) if result.get("ok") else None
        if (item is None or item["status"] not in _CLOSED or not active(st)
                or st.item_log is None):
            return result, events
        if getattr(st, "item_log", None) is not None:
            _sync_edits(st)
            if getattr(st, "tree_pending", False):
                from ._progress import _refresh_tree
                _refresh_tree(st)
                _sync_edits(st)
        evs, carried = _window(st)
        facts = _facts(st, evs)
        status = item["status"]
        suffix = ""
        if status == "done" and verify_enabled():
            ok, why = _verdict(facts)
            if not ok:
                tries = st.item_rejects.get(slug, 0)
                if tries < max_retries():
                    st.item_rejects[slug] = tries + 1
                    item["status"] = "running"
                    hint = _GENERIC_HINTS[min(tries, len(_GENERIC_HINTS) - 1)]
                    result = {"ok": False, "slug": slug, "status": "running",
                              "error": f"not accepted as done: {why}.",
                              "next_step": hint,
                              "open_count": len(open_items(st.board))}
                    return result, [{"type": "subtask_update", "slug": slug,
                                     "status": "running"}]
                if facts["test"] and facts["test"][1] == "red":
                    item["status"] = status = "failed"
                    suffix = f" — verification failed after retries: {why}"
                    result = {**result, "status": "failed",
                              "note": "marked failed: " + why}
                    events = [{"type": "subtask_update", "slug": slug,
                               "status": "failed"}]
                else:
                    suffix = " — UNVERIFIED (accepted after retries)"
        item["note"] = _note(facts, status, suffix)
        if not carried:
            st.item_prev_start = st.item_start
        st.item_start = len(st.item_log)
        st.item_last_closed = slug
        st.item_fresh_close = True
        if reset_enabled() and open_items(st.board):
            st.item_reset_pending = True
        return result, events
    except Exception:  # noqa: BLE001 — the board must keep working
        return result, events


def _next_open(board: dict):
    left = open_items(board)
    running = [s for s in left if board[s]["status"] == "running"]
    pick = (running or left or [None])[0]
    return pick, (board[pick]["title"] if pick else "")


def _reset_message(st) -> str:
    closed = st.item_last_closed
    done_line = ""
    if closed and closed in st.board:
        done_line = (f"Finished just now: {closed}: {st.board[closed]['title']}"
                     f" — {st.board[closed].get('note', '')}\n")
    slug, title = _next_open(st.board)
    return (
        "[harness — not the user] CONTEXT RESET between task-board items. The "
        "working context of the finished item(s) was cleared so the next one "
        "starts fresh and small. What carries over is in the system message and the context note: "
        "the original task, your task board with a result note for every "
        "finished item (written by the harness from what really ran and "
        "changed), and the files on disk.\n"
        + done_line
        + (f"Do next: {slug}: {title}\n" if slug else "")
        + "Work only on that item. Mark it running with plan_progress, do it, "
        "run the test or build that proves it, then mark it done. Re-read "
        "files instead of guessing; the cleared transcript is saved and "
        "memory_lookup restores it if you truly need it.")


def at_step_start(st):
    """Run at the start of every real model step (not a queued call of the
    last reply): the context reset when one is due. Yields UI events."""
    try:
        if reset_pending_ready(st) and reset_context(st):
            yield {"type": "thought", "role": "system",
                   "text": "🧹 item finished — cleared the working context for "
                           "the next board item"}
    except Exception:  # noqa: BLE001 — a failed reset keeps the old context
        pass
    st.item_fresh_close = False


def reset_pending_ready(st) -> bool:
    return (getattr(st, "item_reset_pending", False) and reset_enabled()
            and not getattr(st, "pending_steps", None)
            and not getattr(st, "batch_unread", False)
            and bool(open_items(st.board)))


def reset_context(st) -> bool:
    """Replace the conversation with: system prompt + pinned task + board with
    result notes + a message naming the next item. True when it was reset."""
    st.item_reset_pending = False
    convo = st.convo
    if not convo or convo[0].get("role") != "system" or len(convo) < 3:
        return False
    st.item_resets += 1            # turn_pin keeps the whole goal from now on
    from aiforge_core.runtime import context_offload

    from .._context._compaction import (
        _block_gen,
        _pin_goal,
        _prior_block,
        _stripped_system,
        condense_block,
    )
    from ._batch import _cancel_early_reads
    from ._tasks import pin_board, turn_pin
    from .._context import _note
    in_note = _note.note_index(convo) is not None
    prior_src = _note.text(convo) if in_note else (convo[0].get("content") or "")
    prior = _prior_block(prior_src)
    gen = _block_gen(prior) + 1
    saved = context_offload.save(context_offload.render(convo[1:]))
    if not saved:
        return False               # never clear the transcript without a saved copy
    where = f'It is saved: memory_lookup {{"id": "{saved}"}} reads it.'
    block = condense_block(
        f"[context reset after a finished task-board "
        f"item (condense #{gen}) — {len(convo) - 1} messages cleared. "
        f"Result notes are on the task board. {where}]")
    if in_note:
        # The note layout: the system message stays byte-identical (the prompt
        # cache keeps its prefix) and the goal, the earlier condense record
        # (failed approaches, files, offload ids) and the board live in the note.
        from ._tasks import render_board
        goal = _pin_goal("", convo, turn_pin(st)).strip()
        note_msg = _note.build(goal, prior.strip() if prior else "", block,
                               render_board(st.board))
        fresh = [convo[0], note_msg, _note.ack()]
    else:
        sys_text = _pin_goal(_stripped_system(convo), convo, turn_pin(st))
        fresh = [{"role": "system", "content": (sys_text + "\n\n" + block).strip()}]
        pin_board(fresh, st.board)
    fresh.append({"role": "user", "content": _reset_message(st)})
    st.convo = fresh
    st.read_sigs_seen.clear()
    st.batch_mark = len(fresh)
    _cancel_early_reads(st)
    return True
