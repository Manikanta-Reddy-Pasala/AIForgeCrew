"""The task board: the run's plan, kept where the model can always see it.

Items come from the parts of a multi-part message (``part-N``) and from the
model's own ``plan_progress`` calls: a new slug with a title adds an item, a
known slug changes its status. A long run condenses its history many times,
and the model's progress calls go with it, so after each condense the board is
pinned into the system message. A FINAL that leaves items open gets a bounded
reminder.
"""
from __future__ import annotations

import os
import re

_BOARD_OPEN = "<<AIFORGE_TASK_BOARD>>"
_BOARD_CLOSE = "<</AIFORGE_TASK_BOARD>>"
_STATUSES = ("pending", "running", "done", "failed", "skipped")
#: Words models use for the same statuses.
_ALIASES = {"todo": "pending", "open": "pending", "queued": "pending",
            "in_progress": "running", "in-progress": "running",
            "started": "running", "active": "running", "doing": "running",
            "completed": "done", "complete": "done", "finished": "done",
            "resolved": "done", "blocked": "failed", "error": "failed",
            "cancelled": "skipped", "canceled": "skipped", "wontfix": "skipped",
            "not_needed": "skipped"}
_CLOSED = frozenset({"done", "failed", "skipped"})
_MARK = {"pending": "[ ]", "running": "[>]", "done": "[x]",
         "failed": "[!]", "skipped": "[-]"}
#: Items a model may add in one run. A plan is a handful of lines; this only
#: stops a confused model from growing the pinned block without end.
_MAX_ITEMS = 60
_MAX_TITLE = 200


def seed_board(asks) -> dict:
    """A board holding the parts of a multi-part message."""
    return {f"part-{i + 1}": {"title": str(a)[:_MAX_TITLE], "status": "pending",
                              "from_request": True}
            for i, a in enumerate(asks or [])}


def board_items(board: dict) -> list[dict]:
    """The board as the UI's subtasks dock reads it."""
    return [{"slug": s, "goal": it["title"], "title": it["title"],
             "status": it["status"]} for s, it in board.items()]


def open_items(board: dict) -> list[str]:
    return [s for s, it in board.items() if it["status"] not in _CLOSED]


def open_planned(board: dict) -> list[str]:
    """Open items the model put on the board itself. The parts of a
    multi-part message have their own completeness check at FINAL."""
    return [s for s in open_items(board) if not board[s].get("from_request")]


def apply_progress(board: dict, args: dict) -> tuple[dict, list[dict]]:
    """Apply one ``plan_progress`` call. Returns ``(result, ui_events)``."""
    slug = str(args.get("slug") or args.get("part") or "").strip()[:80]
    title = " ".join(str(args.get("title") or args.get("goal") or "").split())
    status = str(args.get("status") or "").strip().lower().replace(" ", "_")
    status = _ALIASES.get(status, status)
    if not slug:
        return {"ok": False, "error": "missing 'slug'"}, []
    if status and status not in _STATUSES:
        return {"ok": False, "slug": slug,
                "error": f"status must be one of {', '.join(_STATUSES)}"}, []
    if slug not in board and not title:
        # Just a progress flip for the dock (the old convention); only an
        # item with a title goes on the board.
        return ({"ok": True, "slug": slug, "status": status or "done"},
                [{"type": "subtask_update", "slug": slug,
                  "status": status or "done"}])
    added = slug not in board
    if added:
        if len(board) >= _MAX_ITEMS:
            return {"ok": False, "slug": slug,
                    "error": f"the task board is full ({_MAX_ITEMS} items)"}, []
        board[slug] = {"title": title[:_MAX_TITLE], "status": status or "pending"}
    else:
        if title:
            board[slug]["title"] = title[:_MAX_TITLE]
        board[slug]["status"] = status or ("done" if not title else
                                           board[slug]["status"])
    item = board[slug]
    still_open = open_items(board)
    result = {"ok": True, "slug": slug, "status": item["status"],
              "open": [f"{s}: {board[s]['title']}" for s in still_open[:20]],
              "open_count": len(still_open)}
    if title:
        # The dock needs the whole list to show a new or renamed item.
        return result, [{"type": "subtasks", "items": board_items(board)}]
    return result, [{"type": "subtask_update", "slug": slug,
                     "status": item["status"]}]


def render_board(board: dict) -> str:
    lines = [f"{_MARK[it['status']]} {s}: {it['title']}"
             for s, it in board.items()]
    left = len(open_items(board))
    return (f"{_BOARD_OPEN}\nYOUR TASK BOARD ({left} of {len(board)} still open; "
            "[x] done, [>] running, [ ] pending, [!] failed, [-] skipped). "
            "This is the board as of the last condense of older messages; a "
            "plan_progress result after this point is newer and wins. Keep it "
            "up to date and continue with the next open item:\n"
            + "\n".join(lines) + f"\n{_BOARD_CLOSE}")


_BOARD_RE = re.compile(r"\s*" + re.escape(_BOARD_OPEN) + r".*?"
                       + re.escape(_BOARD_CLOSE), re.S)


def pin_board(convo: list[dict], board: dict) -> None:
    """Put the current board at the end of the system message, replacing the
    one pinned before."""
    if not board or not convo or convo[0].get("role") != "system":
        return
    text = convo[0].get("content")
    if not isinstance(text, str):
        return
    text = _BOARD_RE.sub("", text).rstrip()
    convo[0] = {**convo[0], "content": text + "\n\n" + render_board(board)}


def _shown_path(path: str, cwd) -> str:
    try:
        return os.path.relpath(path, cwd) if cwd else path
    except ValueError:            # another drive on Windows
        return path


def turn_pin(st) -> str | None:
    """What a condense must keep in view: this turn's task, the instructions
    the user sent while it ran, and the files it has changed so far. None
    when the turn has no task text (the compactor then pins its own)."""
    goal = (getattr(st, "goal", "") or "").strip()
    if not goal:
        return None
    parts = ["ORIGINAL TASK (stay on this until it's fully done + verified):",
             goal[:1200]]
    if getattr(st, "unlimited", False):
        # Only a run long enough to be condensed gets this reminder.
        from .._prompt import LONG_RUN_RULE
        parts.append(LONG_RUN_RULE)
    steers = [" ".join(s.split())[:300] for s in getattr(st, "steers", [])][-5:]
    if steers:
        parts.append("LATER INSTRUCTIONS FROM THE USER (newest last; they "
                     "override the task where they differ):")
        parts += [f"- {s}" for s in steers]
    changed = [_shown_path(p, st.cwd) for p in getattr(st, "file_hashes", {})]
    if changed:
        more = f" (+{len(changed) - 40} more)" if len(changed) > 40 else ""
        parts.append("FILES CHANGED SO FAR: " + ", ".join(changed[-40:]) + more)
    return "\n".join(parts)


#: Reminders in a row a FINAL gets while task-board items are still open.
_BOARD_NUDGES = 2


def board_nudge_allowed(st) -> bool:
    """Up to _BOARD_NUDGES reminders without progress in between; closing
    an item gives the next premature FINAL its reminders back."""
    closed = len(st.board) - len(open_items(st.board))
    if closed != getattr(st, "board_closed_mark", None):
        st.board_closed_mark = closed
        st.board_nudges = 0
    if st.board_nudges >= _BOARD_NUDGES:
        return False
    st.board_nudges += 1
    return True


def unfinished_reminder(board: dict) -> str:
    items = "\n".join(f"- {s}: {board[s]['title']} ({board[s]['status']})"
                      for s in open_planned(board))
    return ("[task board — not the user] These items on your task board are "
            f"still open:\n{items}\nDo the remaining work now, marking each "
            "item with plan_progress as you go. If an item cannot be done or is "
            "no longer needed, mark it failed or skipped and say why in your "
            "FINAL.")
