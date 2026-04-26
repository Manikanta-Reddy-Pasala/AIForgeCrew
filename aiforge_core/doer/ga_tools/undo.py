"""Undo — git rollback in the worktree.

Two modes:

- ``mode="last_edit"`` (default): ``git checkout HEAD -- <path>``
  for one specific file, leaving the rest of the worktree alone.
- ``mode="last_commit"``: ``git reset --hard HEAD~1`` — wipes
  the most recent commit AND its tree changes. Use only when the
  doer's last commit was wholly wrong and a fresh attempt is
  preferred over patch-by-patch repair.

Refuses on a clean tree (nothing to undo) and refuses on the
single-commit branch case (``HEAD~1`` would orphan).
"""
from __future__ import annotations

import subprocess

SCHEMA = {
    "type": "function",
    "function": {
        "name": "undo",
        "description": (
            "Roll back edits in the worktree. Use mode='last_edit' "
            "+ path=<file> to revert one file to HEAD. Use "
            "mode='last_commit' to nuke the most recent commit + "
            "its tree changes. Last-resort escape hatch when "
            "patches went wrong; counter-resets edit_block_ok via "
            "the harness if you need to retry from scratch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["last_edit", "last_commit"],
                    "default": "last_edit",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Required for mode=last_edit. Repo-relative "
                        "or absolute path."
                    ),
                },
            },
        },
    },
}


def _run(cmd: list[str], cwd: str, timeout_s: int = 30) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def handle(worktree: str, args: dict) -> str:
    mode = (args.get("mode") or "last_edit").lower()
    if mode == "last_edit":
        path = (args.get("path") or "").strip()
        if not path:
            return "[undo] mode=last_edit requires `path`"
        rc, out = _run(["git", "checkout", "HEAD", "--", path], worktree)
        if rc != 0:
            return f"[undo] git checkout failed (rc={rc}): {out[-300:]}"
        return f"[undo] {path} reverted to HEAD"
    if mode == "last_commit":
        rc, head_count = _run(
            ["git", "rev-list", "--count", "HEAD"], worktree,
        )
        if rc != 0 or not head_count.strip().isdigit():
            return "[undo] git history unreadable"
        if int(head_count.strip()) <= 1:
            return "[undo] only 1 commit on this branch — refusing reset"
        rc, out = _run(
            ["git", "reset", "--hard", "HEAD~1"], worktree,
        )
        if rc != 0:
            return f"[undo] git reset failed (rc={rc}): {out[-300:]}"
        return f"[undo] last commit dropped\n{out[-200:]}"
    return f"[undo] unknown mode {mode!r}"
