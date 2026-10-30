"""What changed the course of a chat's work, in one line each, for the user.

The task list says which item runs; it does not say why the list looks the
way it does. A message sent mid-run that retitled two items, a goal the user
dropped, an item the agent skipped or added on its own, a choice the agent
made ("used sqlite, no postgres on this box") — each was a thought row inside
"N agent steps", folded away when the turn ended, so the user had to ask
"what is the status?" to find out.

This module keeps those lines outside the history; the Run overview panel
(``GET /api/chat/sessions/{id}/overview``) shows them beside the goals and
the task list. ``by`` says whose call it was: ``you`` (a message of the
user's), ``agent`` (the model's own plan_progress), ``harness``.

Store: ``<chat scratch>/session-<id>/.aiforge/decisions.json`` (a temporary
chat keeps it in memory only). Off switch: ``AIFORGE_CHAT_DECISIONS=0``.
Soft-fail everywhere: a broken store never breaks a turn.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("aiforge.chat_decisions")

#: Lines kept per chat (the oldest go first).
MAX_ROWS = 150
_TEXT_CHARS = 400

_LOCK = threading.Lock()
_MEM: dict = {}          # temporary chats: session id -> rows


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_DECISIONS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _path(session_id) -> str:
    from aiforge_core.runtime import repo_ident
    return os.path.join(repo_ident.chat_scratch_root(), f"session-{session_id}",
                        ".aiforge", "decisions.json")


def _temporary(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_private
        return bool(chat_private.is_temporary(session_id))
    except Exception:  # noqa: BLE001
        return False


def _load(session_id) -> list:
    if _temporary(session_id):
        return list(_MEM.get(str(session_id)) or [])
    try:
        with open(_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("text")]


def _save(session_id, rows: list) -> None:
    if _temporary(session_id):
        _MEM[str(session_id)] = rows
        return
    from aiforge_core.config import _atomic
    path = _path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps({"rows": rows}, indent=1))


def _user_turns(session_id) -> "int | None":
    try:
        from aiforge_core.runtime import chat_store
        return sum(1 for r in chat_store.get_messages(session_id)
                   if r.get("role") == "user")
    except Exception:  # noqa: BLE001
        return None


def record(session_id, lines, *, by: str, kind: str, why: str = "") -> None:
    """Add one row per line. ``kind``: goal / plan / skip / fail / choice /
    left. ``why``: what led to it (the user's message, in their words)."""
    if session_id is None or not enabled():
        return
    items = [lines] if isinstance(lines, str) else list(lines or [])
    items = [" ".join(str(x).split())[:_TEXT_CHARS] for x in items]
    items = [x for x in items if x]
    if not items:
        return
    try:
        turn = _user_turns(session_id)
        now = time.time()
        why = " ".join(str(why or "").split())[:_TEXT_CHARS]
        with _LOCK:
            rows = _load(session_id)
            for text in items:
                row = {"t": now, "by": by, "kind": kind, "text": text}
                if why and why.lower() not in text.lower():
                    row["why"] = why          # not when the line already says it
                if turn is not None:
                    row["turn"] = turn
                last = rows[-1] if rows else {}
                if (last.get("by"), last.get("kind"), last.get("text")) == (by, kind, text):
                    continue          # the same line again (a repeated decision arg)
                rows.append(row)
            _save(session_id, rows[-MAX_ROWS:])
    except Exception as exc:  # noqa: BLE001
        log.debug("decision write failed (session=%s): %s", session_id, exc)


def rows(session_id) -> list:
    """The chat's rows, oldest first (empty when off)."""
    if session_id is None or not enabled():
        return []
    try:
        with _LOCK:
            return _load(session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("decision read failed (session=%s): %s", session_id, exc)
        return []


def drop_after_turn(session_id, turn: int) -> None:
    """The chat was cut back to ``turn`` user messages (edit-and-resend):
    rows from the turns that are gone go with them."""
    if session_id is None:
        return
    try:
        with _LOCK:
            kept = [r for r in _load(session_id)
                    if not (isinstance(r.get("turn"), int) and r["turn"] > turn)]
            _save(session_id, kept)
    except Exception as exc:  # noqa: BLE001
        log.debug("decision write failed (session=%s): %s", session_id, exc)


def forget(session_id) -> None:
    """The chat was deleted: drop its rows."""
    if session_id is None:
        return
    with _LOCK:
        _MEM.pop(str(session_id), None)
        try:
            os.remove(_path(session_id))
        except OSError:
            pass


def board_lines(changes) -> list:
    """The task-list revision lines of ``_plan_llm.revise`` ("added msg-1:
    …", "removed s2: …") in the user's terms."""
    out = []
    for c in changes or []:
        c = str(c)
        for raw, said in (("added ", "Added task "), ("removed ", "Removed task "),
                          ("changed ", "Changed task "),
                          ("to do again ", "To do again: "),
                          ("new order: ", "New task order: ")):
            if c.startswith(raw):
                c = said + c[len(raw):]
                break
        out.append(c)
    return out


def goal_lines(changes) -> list:
    """``chat_goals`` change lines ("dropped g1: …") in the user's terms."""
    out = []
    for c in changes or []:
        c = str(c)
        for raw, said in (("dropped ", "Goal dropped "), ("changed ", "Goal changed "),
                          ("reopened ", "Goal reopened "), ("added ", "Goal added ")):
            if c.startswith(raw):
                c = said + c[len(raw):]
                break
        out.append(c)
    return out


__all__ = ["MAX_ROWS", "board_lines", "drop_after_turn", "enabled", "forget",
           "goal_lines", "record", "rows"]
