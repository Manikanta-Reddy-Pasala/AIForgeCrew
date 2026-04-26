"""Edit verify — return diff after each file_patch so model sees the result.

GA's ``do_file_patch`` returns ``[Status] OK`` on success without
showing what landed. Model can't tell if the patch caught the
right block, leaks line-ending fixes, or drifted indentation.

This module wraps the post-patch path: takes the ``abs_path`` of
the just-patched file + the old git HEAD content, returns a
unified diff string the doer prepends to its tool result. Mirrors
Claude Code's Edit which shows the new file context.

Pure-function — caller (handler) decides when to compute diff.
"""
from __future__ import annotations

import os
import subprocess


def diff_against_head(worktree: str, rel_path: str,
                      max_lines: int = 80) -> str:
    """Run ``git diff HEAD -- <path>`` in the worktree, truncated.

    Returns the diff as text (with `--- a/...` / `+++ b/...`
    headers). Empty string when nothing changed (file_patch was a
    no-op) or git unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-color", "HEAD", "--", rel_path],
            cwd=worktree, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    diff = proc.stdout
    if not diff.strip():
        return ""
    lines = diff.splitlines()
    if len(lines) > max_lines:
        diff = "\n".join(lines[:max_lines]) + (
            f"\n... [truncated, {len(lines) - max_lines} more lines]"
        )
    return diff


def banner_for(abs_path: str, worktree: str) -> str:
    """Return ``[Edit verify] <rel_path> diff:\\n<diff>`` (or empty).

    Convenience wrapper for the handler — keeps the formatting in
    one place so future tweaks (e.g. inline syntax highlighting,
    comment stripping) live here.
    """
    if not os.path.isfile(abs_path):
        return ""
    rel_path = os.path.relpath(abs_path, worktree)
    diff = diff_against_head(worktree, rel_path)
    if not diff:
        return f"[Edit verify] {rel_path}: no diff vs HEAD"
    return f"[Edit verify] {rel_path}\n{diff}"
