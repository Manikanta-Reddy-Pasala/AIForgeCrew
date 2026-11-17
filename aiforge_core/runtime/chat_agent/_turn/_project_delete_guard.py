"""Deleting a file the project already had is the user's call.

Owner, 11-16: the agent may decide things on its own when the user does not
answer — "not a deletion one". On a real Rust run (approvals off) the model ran
`rm tests/emoji-test.txt && python3 scripts/unicode.py`, setting confirm_delete
itself: a 5,518-line tracked test-data file was gone, it could not be fetched
back (no web access), and three tests that read it failed (36/36 -> 33/36).

A shell command that would delete a file the repository TRACKS (it was there
before this turn; the turn's own new files and build output are not tracked)
is refused unless the request itself asks to delete or remove something —
whatever the approval mode, and whatever confirm_delete the model sets.
Off: ``AIFORGE_CHAT_KEEP_PROJECT_FILES=0``.
"""
from __future__ import annotations

import os
import re
import subprocess

_ASKS_DELETE = re.compile(
    r"\b(?:delete|remove|drop|get rid of|clean ?up|prune|purge|wipe|erase|rm)\b", re.I)


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_KEEP_PROJECT_FILES", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _top(cwd: str) -> "str | None":
    try:
        p = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _tracked(top: str, paths: list) -> list:
    if not paths:
        return []
    rels = [os.path.relpath(p, top) for p in paths]
    try:
        p = subprocess.run(["git", "-C", top, "ls-files", "--", *rels],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    listed = set(p.stdout.splitlines()) if p.returncode == 0 else set()
    out = []
    for rel in rels:
        if rel in listed or any(x.startswith(rel.rstrip("/") + "/") for x in listed):
            out.append(rel)
    return out


def refusal(st, name: str, args: dict, cwd: str = ""):
    """The result to return INSTEAD of running this call, or None."""
    if not _on():
        return None
    try:
        from aiforge_core.runtime.shell_writes import SHELL_TOOLS, command_of
        from aiforge_core.runtime.tools import delete_guard
        if name not in SHELL_TOOLS:
            return None
        if _ASKS_DELETE.search(str(getattr(st, "goal", "") or "")):
            return None
        cmd = command_of(args or {}) or ""
        top = _top(str(getattr(st, "cwd", "") or cwd or "."))
        if not top:
            return None
        hit = _tracked(top, delete_guard.inside_targets(cmd, top))
        if not hit:
            return None
        return {"ok": False, "blocked": "delete_project_file", "files": hit[:6],
                "error": ("refused: this deletes files the project already has ("
                          + ", ".join(hit[:6]) + "), and the request does not ask to "
                          "delete anything — that is the user's call. Leave them in place: "
                          "change the code instead, or move a file aside with `git mv` / "
                          "a copy if you only need it out of the way. Ask the user if it "
                          "must go.")}
    except Exception:  # noqa: BLE001 — a guard never breaks a step on its own error
        return None
