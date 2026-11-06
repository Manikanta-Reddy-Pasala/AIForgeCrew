"""Where each chat stands, in one line for the chat list.

The list showed a title and a time: to know whether a chat finished, stopped
half way, or waits for an answer, the user had to open it. This keeps one
short line per chat:

* running — what it is doing now ("editing files", "running `pytest`");
* waiting for your reply;
* working in the background (a watch, a scheduled run, a side task);
* stopped — at which task of the list;
* done — how many tasks, how many files changed; or done with tasks left;
* failed — the start of the error.

The finished states are worked out once, when a turn is saved (from the
turn's own events, never the transcript), and kept in
``$AIFORGE_CONFIG_DIR/chat-glance.json``; live states come from the run
itself. Best-effort: a line that cannot be made is simply absent.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("aiforge.chat_glance")
_LOCK = threading.Lock()
_KEEP = 1000
_LEFT = ("left", "failed", "cancelled")


def _path() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(os.path.expanduser(str(config_dir())), "chat-glance.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _edited(steps: list) -> int:
    """Files this turn's own edits touched (its evidence log); else the
    turn's change list."""
    ev = next((s.get("evidence") or {} for s in reversed(steps) if s.get("type") == "turn_evidence"), {})
    paths = {st.get("what") for st in (ev.get("steps") or [])
             if isinstance(st, dict) and st.get("kind") == "edit" and st.get("ok") and st.get("what")}
    if paths:
        return len(paths)
    return next((len(s.get("files") or []) for s in reversed(steps) if s.get("type") == "changes"), 0)


def summarize(steps, final_text: str, awaiting: bool, cancelled: bool) -> "dict | None":
    """The line for a turn that just ended, from its persisted steps."""
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    items = next((s.get("items") or [] for s in reversed(steps) if s.get("type") == "subtasks"), [])
    items = [i for i in items if isinstance(i, dict)]
    files = _edited(steps)
    done = sum(1 for i in items if i.get("status") in ("done", "won", "skipped"))
    failed = [i for i in items if i.get("status") == "failed"]
    left = [i for i in items if i.get("status") in ("left", "cancelled")]
    n = len(items)
    text = (final_text or "").strip()
    stopped = cancelled or text.startswith("(stopped") or any(s.get("type") == "stopped" for s in steps)
    errored = any(s.get("type") == "error" for s in steps)
    files_txt = f" · {files} file{'s' if files != 1 else ''} changed" if files else ""
    if awaiting or any(s.get("type") == "awaiting" for s in steps):
        return {"kind": "waiting", "text": "Waiting for your reply"}
    if any(s.get("type") == "plan_ready" for s in steps):
        return {"kind": "waiting", "text": "Plan ready — approve to run it"}
    if stopped:
        at = next((k for k, i in enumerate(items, 1) if i.get("status") == "running"), None) \
            or next((k for k, i in enumerate(items, 1)
                     if i.get("status") not in ("done", "won", "skipped")), None)
        where = f" at task {at} of {n}" if at and n > 1 else ""
        return {"kind": "stopped", "text": f"Stopped{where}{files_txt}"}
    if text.startswith(("⚠", "error:")) or (errored and not text):
        first = " ".join(text.lstrip("⚠\ufe0f ").split())[:70]
        return {"kind": "failed", "text": first or "Failed"}
    if not text:
        return None
    if text.startswith("(no final summary"):
        return {"kind": "partial", "text": f"Ended without a summary{files_txt}"}
    if failed or left:
        parts = ([f"{len(failed)} failed"] if failed else []) + ([f"{len(left)} left"] if left else [])
        return {"kind": "partial",
                "text": f"Done · {' and '.join(parts)} of {n} task{'s' if n != 1 else ''}{files_txt}"}
    tasks = f" · {done}/{n} tasks" if n > 1 else ""
    if not (tasks or files_txt):
        return {"kind": "done", "text": "Answered"}
    return {"kind": "done", "text": f"Done{tasks}{files_txt}"}


def record(session_id, steps, final_text: str, awaiting: bool, cancelled: bool) -> None:
    """Keep the line of the chat's last turn (never a temporary chat's).
    Never raises."""
    if session_id is None:
        return
    try:
        from aiforge_core.runtime import chat_private, chat_store
        if chat_private.is_temporary(session_id):
            return
        line = summarize(steps, final_text, awaiting, cancelled)
        if not line:
            return
        # the chat's message count now: a later message (the user's, a post)
        # makes the line old; a rename does not
        count = len(chat_store.get_messages(session_id) or [])
        line = {**line, "msgs": count}
        from aiforge_core.config import _atomic
        with _LOCK:
            data = _load()
            data[str(session_id)] = {**line, "at": time.time()}
            if len(data) > _KEEP:
                for k, _ in sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[:len(data) - _KEEP]:
                    data.pop(k, None)
            _atomic.write_text(_path(), json.dumps(data))
    except Exception as exc:  # noqa: BLE001
        log.debug("chat glance not kept: %s", exc)


def _live(session_id) -> "dict | None":
    """A running chat: what it is doing right now."""
    try:
        from aiforge_core.runtime import chat_runs, chat_status
        run = chat_runs.get(session_id)
        if run is None or run.done:
            return None
        snap = chat_status.snapshot(run)
        if "approval" in str(snap.get("phase") or ""):
            return {"kind": "waiting", "text": "Waiting for your approval"}
        if snap["in_flight"]:
            c = snap["in_flight"][0]
            what = chat_status.describe_activity(c["name"], c.get("args") or "")
        else:
            what = snap.get("phase") or "thinking"
        done = snap.get("steps_done") or 0
        return {"kind": "running", "text": what[:70] + (f" · {done} steps" if done else "")}
    except Exception:  # noqa: BLE001
        return None


def lines(rows: list, background: set) -> dict:
    """``{session id: line}`` for the chat list: live first, then background,
    then the last turn's outcome."""
    saved = _load()
    out = {}
    for r in rows:
        try:
            line = _line(r, saved, background)
        except Exception:  # noqa: BLE001 — one bad entry blanks only its own row
            line = None
        if line:
            out[r.get("id")] = line
    return out


def _line(r: dict, saved: dict, background: set) -> "dict | None":
    sid = r.get("id")
    if r.get("running"):
        return _live(sid) or {"kind": "running", "text": "Working"}
    if sid in background:
        return {"kind": "background", "text": "Working in the background"}
    got = saved.get(str(sid))
    if got and (got.get("msgs") is None or r.get("message_count") is None
                or int(got["msgs"]) >= int(r["message_count"])):
        return {"kind": got.get("kind"), "text": got.get("text")}
    return None


__all__ = ["lines", "record", "summarize"]
