"""Small tasks, one by one, each from a fresh prompt (team / pipeline mode).

The plan's subtasks already run one after another in the chat's single
worktree, each as its own model run. This module adds what makes that a real
one-by-one plan:

* **A board.** Every subtask's run starts from a prompt built from the shared
  spec (the overall goal), a board of ALL subtasks with their status, a
  factual result note for every finished one, and only THIS subtask's spec.
  It never carries the previous subtask's transcript.
* **Factual result notes.** A finished subtask's note comes from git and the
  validator (files changed with line counts, how it was validated), never
  from what the model said about its own work.
* **Evidence.** A subtask is only done when its commit really changed files
  (``AIFORGE_SUBTASK_EVIDENCE``, default on): an agent that "finished" with no
  diff is retried through the normal retry path, not accepted.
* **Shape.** Every subtask gets an acceptance line (derived from its path
  when the planner gave none), and one that is too big to finish and check in
  one pass is reported.
"""
from __future__ import annotations

import os
from collections import OrderedDict

_MARK = {"pending": "[ ]", "running": "[>]", "done": "[x]", "failed": "[!]"}
_NOTE_MAX = 300
_FILES_SHOWN = 8
_BIG_GOAL_CHARS = 700
_BOARD_MAX = 5000


def _flag(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in (
        "0", "false", "off", "no", "")


def evidence_enabled() -> bool:
    return _flag("AIFORGE_SUBTASK_EVIDENCE")


def board_enabled() -> bool:
    return _flag("AIFORGE_SUBTASK_BOARD")


def new_board(subs) -> dict:
    """``{slug: {title, status, note}}`` in plan order."""
    board: OrderedDict = OrderedDict()
    for s in subs or []:
        if isinstance(s, dict) and s.get("slug"):
            board[s["slug"]] = {
                "title": " ".join(str(s.get("goal") or s.get("slug")).split())[:160],
                "status": "pending", "note": ""}
    return board


def render_board(board: dict, current: str = "") -> str:
    lines = []
    for slug, it in board.items():
        marker = ">>>" if slug == current else _MARK.get(it["status"], "[ ]")
        lines.append(f"{marker} {slug}: {it['title']}")
        if it.get("note") and slug != current:
            lines.append(f"      result: {it['note']}")
    text = "\n".join(lines)
    if len(text) > _BOARD_MAX:
        text = text[:_BOARD_MAX] + "\n…(board truncated)"
    return text


def mark(board: dict, slug: str, status: str, note: str = "") -> None:
    if slug in board:
        board[slug]["status"] = status
        board[slug]["note"] = note[:_NOTE_MAX]


def with_board(subtask: dict, board: dict) -> dict:
    """The subtask as its fresh run sees it: the board rides on a private key
    the prompt builders read. Nothing else carries over."""
    if not board_enabled() or not board or subtask.get("slug") not in board:
        return subtask
    return {**subtask, "_board": render_board(board, subtask["slug"])}


def board_block(subtask: dict) -> str:
    text = str((subtask or {}).get("_board") or "").strip()
    if not text:
        return ""
    return ("TASK BOARD (the whole job; you do ONLY the item marked >>>. "
            "Finished items are already committed on disk and their result "
            "notes come from git, not from the agents; do not redo or "
            "rewrite them):\n" + text + "\n\n---\n\n")


def facts_note(repo_root: str, before: str, result: dict, git) -> str:
    """The result note of a subtask that was accepted: files it changed since
    ``before`` with line counts (git), and how it was validated."""
    bits = []
    try:
        out = git(["diff", "--numstat", before, "HEAD"], repo_root).stdout or ""
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                add, rem, path = parts
                rows.append(f"{path} (+{add} -{rem})")
        if rows:
            more = len(rows) - _FILES_SHOWN
            bits.append("files: " + ", ".join(rows[:_FILES_SHOWN])
                        + (f" (+{more} more)" if more > 0 else ""))
    except Exception:  # noqa: BLE001 — a note is never worth a failed run
        pass
    val = (result or {}).get("validation") or {}
    if isinstance(val, dict) and val.get("via"):
        bits.append(f"validated via {val['via']}")
    attempts = (result or {}).get("attempts")
    if isinstance(attempts, int) and attempts > 1:
        bits.append(f"{attempts} attempts")
    return ("; ".join(bits) or "no file changes recorded")[:_NOTE_MAX]


def failure_note(result: dict) -> str:
    err = str((result or {}).get("error") or "").strip().splitlines()
    return ("FAILED: " + (err[-1] if err else "validation did not pass"))[:_NOTE_MAX]


def changed_since(repo_root: str, before: str, git) -> bool:
    """Did the subtask's work change any file? Committed since ``before`` or
    still uncommitted."""
    try:
        if (git(["diff", "--name-only", before, "HEAD"], repo_root).stdout
                or "").strip():
            return True
        return bool((git(["status", "--porcelain"], repo_root).stdout
                     or "").strip())
    except Exception:  # noqa: BLE001 — no signal is not "no change"
        return True


def evidence_validator(validate_one, repo_root: str, before: str, git):
    """``validate_one`` plus the evidence rule: the subtask's own validation
    must pass AND something must really have changed."""
    def _validate(subtask, wt):
        res = (validate_one(subtask, wt) if validate_one is not None else {}) or {}
        if res.get("ok", True) is False or not evidence_enabled():
            return res
        if before and not changed_since(repo_root, before, git):
            return {"ok": False, "via": "evidence",
                    "error": "this subtask changed no file: nothing shows it "
                             "was done. Make the change the subtask asks for."}
        return res
    return _validate


def default_acceptance(subtask: dict) -> str:
    path = str(subtask.get("path") or "").strip()
    if not path:
        return ""
    if "test" in path.lower():
        return (f"{path} exists and holds real, runnable tests (assertions, "
                "not placeholders)")
    return (f"{path} exists, is complete (no stubs or TODO placeholders) and "
            "passes the syntax check; its tests pass when they exist")


def shape_subtasks(subs) -> list[str]:
    """Make every subtask checkable on its own and report the ones too big to
    finish in one pass. Adds a default acceptance line where the planner gave
    none. Returns the warnings."""
    warnings = []
    for s in subs or []:
        if not isinstance(s, dict):
            continue
        slug = s.get("slug") or "?"
        if not s.get("acceptance"):
            line = default_acceptance(s)
            if line:
                s["acceptance"] = [line]
            else:
                warnings.append(f"{slug}: no file and no acceptance check; "
                                "it can only be judged by what it changes")
        goal = str(s.get("goal") or "")
        if len(goal) > _BIG_GOAL_CHARS:
            warnings.append(f"{slug}: its goal is {len(goal)} characters, "
                            "which is big for one pass; a smaller split "
                            "finishes more reliably")
    return warnings
