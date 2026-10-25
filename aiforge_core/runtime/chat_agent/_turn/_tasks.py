"""The task board: the run's plan, kept where the model can always see it.

Items come from the parts of a multi-part message (``part-N``) and from the
model's own ``plan_progress`` calls: a new slug with a title adds an item, a
known slug changes its status. A long run condenses its history many times,
and the model's progress calls go with it, so after each condense the board is
pinned into the condense note (the message after the system prompt). A FINAL that leaves items open gets a bounded
reminder.
"""
from __future__ import annotations

import os
import re

_BOARD_OPEN = "<<AIFORGE_TASK_BOARD>>"
_BOARD_CLOSE = "<</AIFORGE_TASK_BOARD>>"
#: The item that records the chat's repository roles (runtime/repo_roles.SLUG):
#: the harness's record, not work the model added.
_ROLES_SLUG = "repos"
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
#: Status words that mean "take it off the board".
_REMOVE_WORDS = frozenset({"remove", "removed", "delete", "deleted", "drop", "dropped"})
_MARK = {"pending": "[ ]", "running": "[>]", "done": "[x]",
         "failed": "[!]", "skipped": "[-]"}
#: Items a model may add in one run. A plan is a handful of lines; this only
#: stops a confused model from growing the pinned block without end.
_MAX_ITEMS = 60
_MAX_TITLE = 300


_PLAN_STEP = re.compile(r"(?m)^\s*(?:\d+[\.\)]\s+)(.+)$")


def _plan_steps(goal: str) -> list[str]:
    from .._native_prompt import is_plan_execution
    if not is_plan_execution(goal or ""):
        return []
    return [m.group(1).strip()[:_MAX_TITLE]
            for m in _PLAN_STEP.finditer(goal or "")][:_MAX_ITEMS]


def seed_board(asks, goal: str = "") -> dict:
    """A board holding the parts of a multi-part message, or the approved plan."""
    steps = _plan_steps(goal)
    if steps:
        return {f"plan-{i}": {"title": step, "status": "pending",
                              "from_request": True}
                for i, step in enumerate(steps, 1)}
    return {f"part-{i + 1}": {"title": str(a)[:_MAX_TITLE], "status": "pending",
                              "from_request": True}
            for i, a in enumerate(asks or [])}


def steer_note(board: dict) -> str:
    """What a mid-run message means for the task board: the model is shown
    the board as it stands and told to bring it in line first. "" when the
    run has no board."""
    if not board:
        return ""
    lines = "\n".join(f"{_MARK[it['status']]} {s}: {it['title']}"
                      for s, it in board.items())
    return ("[task board — not the user] Your task board as it stands:\n"
            f"{lines}\n"
            "The user's message above may change this plan. Before you carry "
            "on, bring the board in line with it using plan_progress: a new "
            "slug with a title for work it adds (with after/before/position "
            "to put it where it belongs in the order), remove: true for work "
            "it drops, the same slug with a new title for work it changes. "
            "The user watches this list. If the message changes nothing on it, "
            "leave it as it is.")


def revised_note(board: dict, changes: list) -> str:
    """What the harness did to the task board for a mid-run message. "" when
    the run has no board and the message added none."""
    if not board:
        return ""
    lines = "\n".join(f"{_MARK[it['status']]} {s}: {it['title']}"
                      for s, it in board.items())
    did = ("The harness updated your task board for the user's message above:\n"
           + "\n".join(f"- {c}" for c in changes) if changes else
           "The user's message above changes nothing on your task board.")
    return (f"[task board — not the user] {did}\nThe board now:\n{lines}\n"
            "This board replaces any checklist given earlier. Work through it "
            "in this order; each item says how it is checked. "
            "Mark items with plan_progress as you go. If the board misreads "
            "the user, correct it with plan_progress (same slug with a new "
            "title, remove: true, or after/before to move it).")


def board_items(board: dict) -> list[dict]:
    """The board as the UI's subtasks dock reads it."""
    return [{"slug": s, "goal": it["title"], "title": it["title"],
             "status": it["status"], "enforced": bool(it.get("enforced")),
             "self_added": bool(it.get("self_added"))}
            for s, it in board.items()]


def open_items(board: dict) -> list[str]:
    return [s for s, it in board.items() if it["status"] not in _CLOSED]


def open_planned(board: dict) -> list[str]:
    """Open items the model put on the board itself. The parts of a
    multi-part message have their own completeness check at FINAL."""
    return [s for s in open_items(board) if not board[s].get("from_request")]


def open_for_final(st) -> list[str]:
    """The open items a FINAL must not leave behind: the ones the model put on
    the board itself and, once the model has worked the board with
    plan_progress, the parts of the request as well. A multi-part request the
    model answers in one go without touching the board is not held up (it has
    no evidence to mark the parts with)."""
    board = st.board
    if getattr(st, "board_touched", False):
        return open_items(board)
    return open_planned(board)


_STOP = frozenset("a an the to in on of for and or with that this is it be "
                  "by from as at so then each both".split())
_WORD = re.compile(r"[\w./-]+")


def _stem(w: str) -> str:
    for end in ("ing", "es", "ed", "s"):
        if len(w) > len(end) + 3 and w.endswith(end):
            return w[:-len(end)]
    return w


def _words(title: str) -> set:
    head = re.split(r"\s[—–-]+\s*check:|\(done when", title, maxsplit=1,
                    flags=re.I)[0]
    return {_stem(w.strip("./-:").lower()) for w in _WORD.findall(head)} - _STOP - {""}


def context_words(board: dict, goal: str = "", steers=None) -> set:
    """The content words of what the user asked for in this turn: the request,
    every mid-run steer, and the items the harness put on the board (the
    request's parts, a plan's steps, work a later message added). A model-added
    item that shares none of them is work the user did not ask for."""
    out = _words(goal or "")
    for s in steers or ():
        out |= _words(str(s))
    for it in (board or {}).values():
        if it.get("from_request") or it.get("by_harness"):
            out |= _words(it.get("title") or "")
    return out


def _names(words: set) -> set:
    """The words that name a thing: svc-a, notes.md, v2. (A full path is
    where, not what: a reworded title seldom repeats it.)"""
    return {w for w in words if re.search(r"[-.\d]", w) and "/" not in w}


def _same_work(board: dict, title: str) -> str | None:
    """The item the harness put on the board (the request's parts, or work a
    later message added) that ``title`` is plainly a rewording of, else None.
    A model that plans again in its own words would otherwise double the
    board and leave the first copy open. The names in ``title`` (repos,
    files) must be the item's; then the words in common, over all the
    words of both, must be clearly highest for one item."""
    mine = _words(title)
    if len(mine) < 3:
        return None
    scores = []
    for s, it in board.items():
        if not (it.get("from_request") or it.get("by_harness")):
            continue
        theirs = _words(it["title"])
        if _names(mine) and _names(mine) != _names(theirs):
            continue
        scores.append((len(mine & theirs) / len(mine | theirs), s))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.55:
        return None
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.15:
        return None                      # two near matches: not plainly one
    return scores[0][1]


def _known(board: dict, slug: str) -> str:
    """``slug``, or the board item it was mapped onto earlier."""
    if slug in board:
        return slug
    return next((s for s, it in board.items() if slug in it.get("aliases", ())), slug)


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def _reorder(board: dict, order: list) -> list:
    """Put ``board`` in the order of ``order`` (slugs; unknown ones ignored);
    items it leaves out keep their place after the item before them."""
    known = [_known(board, str(s).strip()) for s in order]
    known = [s for i, s in enumerate(known) if s in board and s not in known[:i]]
    rest = list(board)
    out = list(known)
    for i, s in enumerate(rest):
        if s in out:
            continue
        before = [x for x in rest[:i] if x in out]
        out.insert(out.index(before[-1]) + 1 if before else 0, s)
    items = {s: board[s] for s in out}
    board.clear()
    board.update(items)
    return out


def _place(board: dict, slug: str, args: dict) -> bool:
    """Move ``slug`` where the call says: ``after`` / ``before`` another item,
    or ``position`` (1 = first). True when it moved."""
    after = _known(board, str(args.get("after") or "").strip())
    before = _known(board, str(args.get("before") or "").strip())
    pos = args.get("position")
    order = [s for s in board if s != slug]
    if after in board and after != slug:
        order.insert(order.index(after) + 1, slug)
    elif before in board and before != slug:
        order.insert(order.index(before), slug)
    elif str(pos).strip().isdigit() and int(pos) >= 1:
        order.insert(min(int(pos), len(order) + 1) - 1, slug)
    else:
        return False
    old = list(board)
    _reorder(board, order)
    return old != list(board)


def _remove(board: dict, slug: str, allow_request: bool) -> "tuple[dict, list]":
    asked = slug
    slug = _known(board, slug)
    if slug not in board:
        return {"ok": False, "slug": asked, "error": "not on your task board; items: "
                + ", ".join(board)[:400]}, []
    if board[slug].get("enforced"):
        return {"ok": False, "slug": slug, "error": (
            f"{slug} is a target item: it stays on the board until a compare "
            "command (diff/cmp/jq/python) on the target file runs and passes.")}, []
    if board[slug].get("from_request") and not allow_request:
        return {"ok": False, "slug": slug, "error": (
            f"{slug} is a part of the user's request: it stays on the board (the "
            "user sees what happened to it). If it is dropped or cannot be done, "
            "mark it status skipped and say why in the title.")}, []
    if board[slug]["status"] in ("done", "failed"):
        return {"ok": False, "slug": slug, "error": (
            f"{slug} is {board[slug]['status']}: finished work stays on the board "
            "as the record of what was done.")}, []
    del board[slug]
    return {"ok": True, "slug": slug, "removed": True,
            "open": [f"{s}: {board[s]['title']}" for s in open_items(board)[:20]],
            "open_count": len(open_items(board))}, \
        [{"type": "subtasks", "items": board_items(board)}]


def _enforced_refusal(board: dict, slug: str, status: str, args: dict) -> dict | None:
    """The result that refuses the model's own ``done`` on an enforced item
    (a target: it closes only when the harness saw a passing compare command,
    which calls with ``_harness``). None when the write may go ahead."""
    if status != "done" or not board[slug].get("enforced") or args.get("_harness"):
        return None
    if board[slug].get("status") == "done":
        return None          # the harness already closed it on a passing compare
    return {"ok": False, "slug": slug, "status": board[slug]["status"],
            "error": "target items close only on a passing compare command "
                     "(diff/cmp/jq/python) on the target file — run one"}


def apply_progress(board: dict, args: dict, *,
                   allow_request_removal: bool = False,
                   context_words: "set | None" = None) -> tuple[dict, list[dict]]:
    """Apply one ``plan_progress`` call. Returns ``(result, ui_events)``.

    Besides adding an item (new slug + title) and moving one (known slug +
    status), a call can REMOVE an item (``remove: true`` or status
    ``removed``), place an item (``after`` / ``before`` a slug, or
    ``position``, 1 = first) and put the whole board in an ``order`` (a list
    of slugs). A part of the user's request is removed only when the user
    dropped it (``allow_request_removal``: a message arrived mid-run).

    ``context_words`` (:func:`context_words`): an item the model adds whose
    title shares no content word with them is tagged ``self_added`` — work
    the user did not ask for, whose files the scope net names at FINAL. A
    model told to write a big request as small tasks on its board shares
    the request's words; it is not tagged. None: no tagging."""
    if isinstance(args.get("order"), list) and not args.get("slug"):
        before = list(board)
        _reorder(board, args["order"])
        return ({"ok": True, "order": list(board),
                 "open": [f"{s}: {board[s]['title']}" for s in open_items(board)[:20]]},
                [{"type": "subtasks", "items": board_items(board)}]
                if list(board) != before else [])
    slug = str(args.get("slug") or args.get("part") or "").strip()[:80]
    title = " ".join(str(args.get("title") or args.get("goal") or "").split())
    status = str(args.get("status") or "").strip().lower().replace(" ", "_")
    if not slug:
        return {"ok": False, "error": "missing 'slug'"}, []
    if _truthy(args.get("remove")) or status in _REMOVE_WORDS:
        return _remove(board, slug, allow_request_removal)
    status = _ALIASES.get(status, status)
    if status and status not in _STATUSES:
        return {"ok": False, "slug": slug,
                "error": f"status must be one of {', '.join(_STATUSES)}"}, []
    asked = slug
    slug = _known(board, slug)
    same = _same_work(board, title) if (slug not in board and title) else None
    if same:
        # Work already on the board: the call moves that item, and the
        # model's slug is kept as another name for it.
        board[same].setdefault("aliases", []).append(asked)
        slug, title = same, ""
        status = status or board[same]["status"]
    if slug not in board and not title:
        # Just a progress flip for the dock (the old convention); only an
        # item with a title goes on the board.
        note = ({"note": "not on your task board; open items: "
                 + ", ".join(open_items(board)[:20])} if open_items(board) else {})
        return ({"ok": True, "slug": slug, "status": status or "done", **note},
                [{"type": "subtask_update", "slug": slug,
                  "status": status or "done"}])
    added = slug not in board
    if added:
        if len(board) >= _MAX_ITEMS:
            return {"ok": False, "slug": slug,
                    "error": f"the task board is full ({_MAX_ITEMS} items)"}, []
        board[slug] = {"title": title[:_MAX_TITLE], "status": status or "pending"}
        mine = _words(title) if context_words is not None else set()
        if (mine and not (mine & context_words) and slug != _ROLES_SLUG
                and not _truthy(args.get("by_harness"))):
            # Work the model put on the board itself: nothing the user asked
            # for (request, steers, harness items) names it. The scope net
            # names the files changed under it at FINAL.
            board[slug]["self_added"] = True
    else:
        placing = any(args.get(k) not in (None, "") for k in ("after", "before", "position"))
        new_status = status or ("done" if not (title or placing) else
                                board[slug]["status"])
        refused = _enforced_refusal(board, slug, new_status, args)
        if refused:
            return refused, []
        if title:
            board[slug]["title"] = title[:_MAX_TITLE]
        board[slug]["status"] = new_status
    moved = _place(board, slug, args)
    item = board[slug]
    still_open = open_items(board)
    result = {"ok": True, "slug": slug, "status": item["status"],
              **({"note": f"already on your task board as {slug}: "
                          f"{item['title']} — use slug {slug} for it"}
                 if slug != asked else {}),
              "open": [f"{s}: {board[s]['title']}" for s in still_open[:20]],
              "open_count": len(still_open)}
    if title or moved or added:
        # The dock needs the whole list to show a new, renamed or moved item.
        result["position"] = list(board).index(slug) + 1
        return result, [{"type": "subtasks", "items": board_items(board)}]
    return result, [{"type": "subtask_update", "slug": slug,
                     "status": item["status"]}]


def render_board(board: dict) -> str:
    lines = []
    for s, it in board.items():
        lines.append(f"{_MARK[it['status']]} {s}: {it['title']}")
        if it.get("note"):
            lines.append(f"      result: {it['note']}")
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
    """Pin the current board, replacing the one pinned before: in the condense
    note when the history has one (the system message stays byte-identical, so
    the model server's prompt cache survives), else at the end of the system
    message."""
    if not board or not convo or convo[0].get("role") != "system":
        return
    from .._context import _note
    at = _note.note_index(convo)
    if at is not None:
        convo[at] = {**convo[at], "content": _note.with_board(
            convo[at]["content"], render_board(board), _BOARD_RE)}
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
    parts: list = []
    if goal:
        from .._native_prompt import is_plan_execution
        limit = 4000 if is_plan_execution(goal) else 1200
        if getattr(st, "item_resets", 0):
            # After a reset between board items the goal is the ONLY copy of
            # the request (its message is gone): keep it whole.
            limit = max(limit, 8000)
        parts += ["ORIGINAL TASK (stay on this until it's fully done + verified):",
                  goal[:limit]]
    if (getattr(st, "unlimited", False) and not getattr(st, "readonly_mode", False)
            and not getattr(st, "builder", None)):
        # Only a run long enough to be condensed gets this reminder.
        from .._prompt_text import LONG_RUN_RULE
        parts.append(LONG_RUN_RULE)
    steers = [" ".join(s.split())[:_STEER_CHARS]
              for s in getattr(st, "steers", [])][-_STEER_KEEP:]
    if steers:
        parts.append("LATER INSTRUCTIONS FROM THE USER (newest last; they "
                     "override the task where they differ):")
        parts += [f"- {s}" for s in steers]
    changed = [_shown_path(p, getattr(st, "cwd", None)) for p in getattr(st, "file_hashes", {})]
    if changed:
        more = f" (+{len(changed) - 40} more)" if len(changed) > 40 else ""
        parts.append("FILES CHANGED SO FAR: " + ", ".join(changed[-40:]) + more)
    try:
        from aiforge_core.runtime import targets as _targets
        tb = _targets.block(getattr(st, "session_id", None))
        if tb:
            parts.append(tb)
    except Exception:  # noqa: BLE001 — the pin stands without the targets
        pass
    parts += [b for b in _repo_blocks(st) if b]
    lessons = _lessons_block(st)
    if lessons:
        parts.append(lessons)
    if not parts:
        return None
    if not goal:
        # No task text (an image-only message): the compactor still pins the
        # request itself; this block carries what must not be lost with it.
        parts.insert(0, "KEEP IN VIEW:")
    return "\n".join(parts)


#: Mid-run instructions the pin keeps, and how much of each.
_STEER_KEEP = 8
_STEER_CHARS = 600


def _repo_blocks(st) -> list:
    """The chat's recorded repositories (WORK IN / READ ONLY) and git targets,
    read NOW: the turn note holds the turn-start copy, and roles recorded
    mid-turn (plan_progress slug=repos) were never shown after a condense."""
    sid = getattr(st, "session_id", None)
    if sid is None:
        return []
    out = []
    try:
        from aiforge_core.runtime import repo_roles
        note = repo_roles.prompt_note(sid, getattr(st, "cwd", None))
        if note.startswith("REPOSITORIES IN THIS CHAT"):
            out.append(note)
    except Exception:  # noqa: BLE001 — the pin stands without them
        pass
    try:
        from aiforge_core.runtime import git_target
        out.append(git_target.prompt_note(sid))
    except Exception:  # noqa: BLE001
        pass
    return out


def _lessons_block(st) -> str:
    """Mistakes this chat already made and the user's corrections, kept
    verbatim (runtime/chat_lessons) so a condense never lets the model repeat
    them."""
    try:
        from aiforge_core.runtime import chat_lessons
        return chat_lessons.block(st)
    except Exception:  # noqa: BLE001
        return ""


#: Reminders in a row a FINAL gets while task-board items are still open.
_BOARD_NUDGES = 2


def board_nudge_allowed(st) -> bool:
    """Up to _BOARD_NUDGES reminders without progress in between; closing
    an item gives the next premature FINAL its reminders back."""
    closed = len(st.board) - len(open_items(st.board))
    if closed > (st.board_closed_mark if st.board_closed_mark is not None else -1):
        st.board_closed_mark = closed
        st.board_nudges = 0
    if st.board_nudges >= _BOARD_NUDGES:
        return False
    st.board_nudges += 1
    return True


def unfinished_reminder(board: dict, slugs=None) -> str:
    items = "\n".join(f"- {s}: {board[s]['title']} ({board[s]['status']})"
                      for s in (open_planned(board) if slugs is None else slugs))
    return ("[task board — not the user] These items on your task board are "
            f"still open:\n{items}\nDo the remaining work now, marking each "
            "item with plan_progress as you go. If an item cannot be done or is "
            "no longer needed, mark it failed or skipped and say why in your "
            "FINAL.")
