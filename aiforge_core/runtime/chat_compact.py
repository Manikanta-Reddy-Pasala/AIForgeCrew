"""Compact a chat's context on request — the "Compact" button.

The context shrinks by itself when the window is nearly full (the condense at
``compact_pct``). The user sometimes wants it now: a long run has filled the
window with old file reads and command output, the model has slowed down, or
a new part of the work should start lean. "Fresh" threw the working context
away; "Compact" keeps what matters (the goal, the task board, a summary of
what was done, the newest turns) and drops the rest.

* A turn is running: :func:`request` marks the chat; the loop takes the mark
  at its next step (:func:`take`), condenses the running conversation right
  there (``force``) and carries on with the same task — nothing is stopped.
* No turn is running: :func:`compact_saved` condenses the working transcript
  the chat carries to its next message (runtime/chat_transcript), waiting a
  bounded time for the model summary. When the last turn did not finish (it
  was stopped, or the harness said work is left) the caller starts a turn
  that continues it (api route).
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("aiforge.chat_compact")

_LOCK = threading.Lock()
_ASKED: dict = {}               # session id → time the user asked
#: Newest messages a requested compaction keeps whole (the auto condense keeps
#: up to 18): the point of pressing the button is a lean window.
KEEP_RECENT = 6


def _wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_CHAT_COMPACT_WAIT_S", "90")))
    except ValueError:
        return 90.0


def request(session_id) -> None:
    """Ask the running turn of ``session_id`` to compact at its next step."""
    with _LOCK:
        _ASKED[str(session_id)] = time.time()


#: A request older than this was not for the turn that is running now.
_TTL_S = 600


def take(session_id) -> bool:
    """The running turn's check: was a compaction asked for? (Clears it.)"""
    if session_id is None:
        return False
    with _LOCK:
        at = _ASKED.pop(str(session_id), None)
    return at is not None and time.time() - at < _TTL_S


def clear(session_id) -> None:
    """The turn ended: a request it never reached is not carried to the next
    turn (it would compact that one at its first step, unasked)."""
    with _LOCK:
        _ASKED.pop(str(session_id), None)


def pending(session_id) -> bool:
    with _LOCK:
        return str(session_id) in _ASKED


def _chars(msgs) -> int:
    n = 0
    for m in msgs or []:
        c = m.get("content") if isinstance(m, dict) else ""
        n += len(c) if isinstance(c, str) else len(str(c))
    return n


#: Newest tool output kept whole by a requested compaction, in tokens.
KEEP_TOKENS = 8_000


def compact_running(st, role, complete_fn, session_id) -> "dict | None":
    """Shrink ``st.convo`` now, for a request: old file reads and command
    output become a head, a tail and an id (as the auto condense does first,
    but keeping only the newest ~8K tokens whole), then the older turns are
    folded into the condense note. Results the model has not read yet are
    kept. ``{before, after, ...}`` in chars, None when nothing shrank."""
    from aiforge_core.runtime.chat_agent._context._aging import age_observations
    from aiforge_core.runtime.chat_agent._context._compaction import _compact_convo
    from aiforge_core.runtime.chat_agent._context._window import _chars_per_token
    from aiforge_core.runtime.chat_agent._turn._batch import _unread_batch_msgs
    from aiforge_core.runtime.chat_agent._turn._limits import _after_condense
    from aiforge_core.runtime.chat_agent._turn._tasks import turn_pin
    before_n, before_c = len(st.convo), _chars(st.convo)
    unread = _unread_batch_msgs(st)
    try:
        age_observations(
            st.convo, protect_from=st.batch_mark if getattr(st, "batch_unread", False) else None,
            forget=getattr(st, "read_sigs_seen", None), force=True,
            keep_chars=int(KEEP_TOKENS * _chars_per_token(role)),
            # The newest result (the model reads it at this very step) and
            # the call that asked for it stay whole; older output is shrunk.
            keep_msgs=2)
    except Exception as exc:  # noqa: BLE001 — the fold below still runs
        log.debug("prune on request skipped: %s", exc)
    st.convo = _compact_convo(st.convo, keep_recent=KEEP_RECENT, role=role,
                              complete_fn=complete_fn, session_id=session_id,
                              force=True, keep_min=unread, pin=turn_pin(st),
                              run_key=getattr(st, "compact_key", None),
                              handoff_st=st)
    _after_condense(st, unread, before_n)
    after_c = _chars(st.convo)
    if after_c >= before_c * 0.97:
        return None
    # Dropped or shortened tool results: a re-read is not a duplicate any more.
    try:
        st.read_sigs_seen.clear()
    except Exception:  # noqa: BLE001
        pass
    return {"before": before_c, "after": after_c,
            "messages_before": before_n, "messages_after": len(st.convo)}


def compact_saved(session_id, role: str = "chat") -> dict:
    """Condense the transcript the chat carries to its next message."""
    from aiforge_core.runtime import chat_transcript
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    from aiforge_core.runtime.chat_agent._context._compaction import (
        _compact_convo,
        release_run,
    )
    from aiforge_core.runtime.chat_agent._context import _note
    work = chat_transcript.load_work(session_id)
    if not work:
        return {"ok": True, "compacted": False,
                "reason": "nothing to compact: this chat carries no working context "
                          "to its next message"}
    if not _note.enabled():
        return {"ok": True, "compacted": False,
                "reason": "compacting the carried context needs the stable prompt "
                          "prefix (AIFORGE_STABLE_PREFIX)"}
    stamp = chat_transcript.stamp(session_id)
    # The goal to pin is the chat's latest request, not the oldest one the
    # transcript happens to start with.
    latest = next((str(m.get("content") or "") for m in reversed(work)
                   if isinstance(m, dict) and m.get("role") == "user"
                   and not str(m.get("content") or "").startswith(("OBSERVATION", "["))), "")
    convo = [{"role": "system", "content": ""}] + work
    key = f"compact-{session_id}-{int(time.time())}"
    # The chat's targets ride the pin too: never summarized away.
    try:
        from aiforge_core.runtime import targets
        _tblock = targets.block(session_id)
    except Exception:  # noqa: BLE001
        _tblock = ""
    # And what the chat learned the hard way, and the repositories it works in.
    _extra = []
    try:
        from aiforge_core.runtime import chat_lessons, repo_roles
        _extra.append(chat_lessons.block(session_id))
        _rr = repo_roles.prompt_note(session_id)
        if _rr.startswith("REPOSITORIES IN THIS CHAT"):
            _extra.append(_rr)
    except Exception:  # noqa: BLE001
        pass
    _task = ("ORIGINAL TASK (the chat's latest request):\n" + latest[:2000]) \
        if latest.strip() else ""
    pin = "\n\n".join(p for p in [_task, _tblock, *_extra] if p).strip() or None
    new = _compact_convo(convo, keep_recent=KEEP_RECENT, role=role,
                         session_id=session_id, force=True, run_key=key,
                         pin=pin)
    if len(new) >= len(convo):
        release_run(key)
        return {"ok": True, "compacted": False,
                "reason": "the working context is already short"}
    # The model summary is written in the background; wait for it (bounded),
    # so the next message starts from a summary, not only a breadcrumb.
    deadline = time.monotonic() + _wait_s()
    while _summary_bg.pending(key) is not None and time.monotonic() < deadline:
        time.sleep(0.5)
    new = release_run(key, new)
    out_work = chat_transcript._working_part(new) or new[1:]
    if not chat_transcript.replace_work(session_id, out_work, expect=stamp):
        return {"ok": True, "compacted": False,
                "reason": "the chat moved on while compacting (a new message was "
                          "answered); nothing was changed — press Compact again"}
    return {"ok": True, "compacted": True, "before": _chars(work),
            "after": _chars(out_work), "messages_before": len(work),
            "messages_after": len(out_work)}


#: What the harness writes into an answer that left work behind.
_UNFINISHED_MARKS = ("this answer does not look finished",
                     "Not done from the task list:")


def last_turn_unfinished(session_id) -> bool:
    """The chat's last turn did not finish: it was stopped, or its answer
    carries the harness's "work is left" note."""
    try:
        from aiforge_core.runtime import chat_resume, chat_store
        rows = chat_store.get_messages(int(session_id)) or []
    except Exception:  # noqa: BLE001
        return False
    if chat_resume.last_stopped_turn(rows):
        return True
    last = next((m for m in reversed(rows) if isinstance(m, dict)
                 and m.get("role") == "assistant"), None)
    text = str((last or {}).get("content") or "")
    return any(mark in text for mark in _UNFINISHED_MARKS)


#: The words of the turn that continues a stopped task (the resume brief is
#: built for exactly this kind of "continue" message).
RESUME_PROMPT = "continue"


__all__ = ["KEEP_RECENT", "RESUME_PROMPT", "clear", "compact_running", "compact_saved",
           "last_turn_unfinished",
           "pending", "request", "take"]
