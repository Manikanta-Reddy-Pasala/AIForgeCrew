"""Bulk edit — apply N file patches atomically, rollback on any failure.

Mirrors Aider's multi-file SEARCH/REPLACE diff format. The model
emits one ``bulk_edit`` call carrying every change for the ticket;
the harness either lands all of them or rolls back the worktree
to HEAD.

Why: GA's stock ``file_patch`` is one-file-at-a-time. For a ticket
that touches controller + service + repo + DTO, the model spends
several turns + token budget on individual patches. Bulk-edit
collapses the work into one round-trip.

Atomicity: we record HEAD's state before the first patch, run
each via the underlying file_patch dispatch, and on failure
``git checkout HEAD -- <path>`` for every file we touched. The
git index is the source of truth, no bespoke rollback math.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable

SCHEMA = {
    "type": "function",
    "function": {
        "name": "bulk_edit",
        "description": (
            "Apply multiple file edits in a single tool call. Each "
            "edit is the same payload as file_patch (path + "
            "old_content + new_content). All-or-nothing — if any "
            "single patch fails, every patched file in this call is "
            "rolled back to git HEAD. Use this when the ticket "
            "touches several files (controller + service + repo + "
            "DTO) — collapses 4-5 turns into one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": (
                        "List of edits. Each item: "
                        "{path: <str>, old_content: <str>, new_content: <str>}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_content": {"type": "string"},
                            "new_content": {"type": "string"},
                        },
                        "required": ["path", "old_content", "new_content"],
                    },
                },
            },
            "required": ["edits"],
        },
    },
}


def _git_checkout(worktree: str, rel_path: str) -> None:
    subprocess.run(
        ["git", "checkout", "HEAD", "--", rel_path],
        cwd=worktree, capture_output=True, timeout=15,
    )


def handle(worktree: str, edits: list[dict],
           apply_one: Callable[[dict], dict]) -> str:
    """Run each edit via ``apply_one``; rollback on any failure.

    ``apply_one(edit) -> {"status": "success"|"error", "msg": ...}``
    matches the GA ``file_patch`` outcome. The handler thin-wraps
    self.do_file_patch in a closure so we don't reimplement the
    diff math here.
    """
    if not edits:
        return "[bulk_edit] empty edits list"
    touched: list[str] = []
    results: list[str] = []
    for i, edit in enumerate(edits):
        rel = edit.get("path", "")
        if not rel:
            return f"[bulk_edit] edit #{i} missing path — refusing whole batch"
        result = apply_one(edit)
        status = (result or {}).get("status", "error")
        if status not in ("success", "ok"):
            # Roll back every file we already touched in this batch.
            for prev in touched:
                _git_checkout(worktree, prev)
            return (
                f"[bulk_edit] FAILED at edit #{i} ({rel}): "
                f"{(result or {}).get('msg', '?')}\n"
                f"Rolled back {len(touched)} previously-applied edit(s). "
                f"Re-emit with corrected old_content for #{i}."
            )
        touched.append(rel)
        results.append(f"#{i} {rel} ✅")
    return f"[bulk_edit] applied {len(edits)} edits atomically:\n" + "\n".join(results)
