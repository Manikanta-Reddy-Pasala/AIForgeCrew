"""smolagents Tool wrappers for the Doer agent.

Tools are created as callables via the smolagents ``@tool`` decorator or
as :class:`~smolagents.Tool` subclasses.  All write tools are scope-checked
via an injected :class:`~aiforge_core.doer.scope_guard.ScopeGuard`.

Factories (``make_*``) close over ``worktree_path`` and ``scope_guard`` so
each agent invocation gets its own correctly-bound copies.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable

from smolagents import tool

from .scope_guard import ScopeGuard, ScopeViolation


# ─────────────────────────── read_file ──────────────────────────────────

def make_read_file(worktree_path: str) -> Callable:
    """Return a ``read_file`` tool bound to *worktree_path*."""

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 file.  Absolute or worktree-relative path.

        Args:
            path: File path (absolute or relative to the worktree root).
        """
        resolved = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except FileNotFoundError:
            return f"ERROR: file not found: {resolved}"
        except OSError as exc:
            return f"ERROR: {exc}"

    return read_file


# ─────────────────────────── edit_block ─────────────────────────────────

def make_edit_block(worktree_path: str, scope_guard: ScopeGuard) -> Callable:
    """Return an ``edit_block`` tool bound to *worktree_path* and *scope_guard*."""

    @tool
    def edit_block(path: str, find: str, replace: str) -> str:
        """Replace an exact substring *find* with *replace* in a file.

        *find* must appear exactly once; errors on 0 or >1 matches.

        Args:
            path: File path (absolute or relative to the worktree root).
            find: Exact text to locate.  Must match uniquely.
            replace: Replacement text.
        """
        resolved = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            scope_guard.check(resolved)
        except ScopeViolation as exc:
            return f"SCOPE_VIOLATION: {exc}"
        if not os.path.exists(resolved):
            return f"ERROR: file not found: {resolved}"
        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        count = src.count(find)
        if count == 0:
            return f"ERROR: find string not found in {resolved}"
        if count > 1:
            return f"ERROR: find string matches {count} times in {resolved}; make it unique"
        new_src = src.replace(find, replace, 1)
        with open(resolved, "w", encoding="utf-8") as fh:
            fh.write(new_src)
        return f"OK: edited {resolved} (-{len(find)} +{len(replace)} chars)"

    return edit_block


# ─────────────────────────── run_compile ────────────────────────────────

def make_run_compile(worktree_path: str) -> Callable:
    """Return a ``run_compile`` tool bound to *worktree_path*."""

    @tool
    def run_compile() -> str:
        """Run ``mvn -q -DskipTests compile`` in the worktree.

        Returns EXIT=N followed by the last 40 lines of output.
        """
        try:
            proc = subprocess.run(
                ["mvn", "-q", "-DskipTests", "compile"],
                cwd=worktree_path,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except FileNotFoundError:
            return "EXIT=1\nERROR: mvn not found on PATH"
        except subprocess.TimeoutExpired:
            return "EXIT=1\nERROR: compile timed out after 300s"
        combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        tail = "\n".join(combined.splitlines()[-40:])
        return f"EXIT={proc.returncode}\n{tail}"

    return run_compile


# ─────────────────────────── grep ───────────────────────────────────────

def make_grep(worktree_path: str) -> Callable:
    """Return a ``grep`` tool bound to *worktree_path*."""

    @tool
    def grep(pattern: str, path: str = "src/main/java") -> str:
        """Search for *pattern* (ripgrep regex) under *path* in the worktree.

        Args:
            pattern: Ripgrep regex pattern.
            path: Sub-path relative to the worktree root (default: src/main/java).
        """
        target = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            proc = subprocess.run(
                ["rg", "-n", "--no-heading", "--color=never", pattern, target],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return "ERROR: rg (ripgrep) not installed"
        except subprocess.TimeoutExpired:
            return "ERROR: grep timed out after 30s"
        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode == 1 and not out:
            return "(no matches)"
        return out[:8000]

    return grep


# ─────────────────────────── list_dir ───────────────────────────────────

def make_list_dir(worktree_path: str) -> Callable:
    """Return a ``list_dir`` tool bound to *worktree_path*."""

    @tool
    def list_dir(path: str = ".") -> str:
        """List directory contents relative to the worktree root.

        Args:
            path: Directory path (relative to worktree or absolute).
        """
        target = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            entries = sorted(os.listdir(target))
        except FileNotFoundError:
            return f"ERROR: directory not found: {target}"
        except NotADirectoryError:
            return f"ERROR: not a directory: {target}"
        return "\n".join(entries) if entries else "(empty)"

    return list_dir


# ─────────────────────────── final_answer ───────────────────────────────

@tool
def final_answer(summary: str) -> str:
    """Signal successful completion.  Returns the summary verbatim.

    Args:
        summary: Human-readable description of what was done.
    """
    return summary


# ─────────────────────────── factory ────────────────────────────────────

def make_tools(worktree_path: str, scope_guard: ScopeGuard) -> list:
    """Build the tool list for a Doer agent invocation.

    ``final_answer`` is intentionally excluded: ``ToolCallingAgent`` adds its
    own ``FinalAnswerTool`` automatically, so adding a second one would
    cause a duplicate-name error at agent construction time.
    """
    return [
        make_read_file(worktree_path),
        make_edit_block(worktree_path, scope_guard),
        make_run_compile(worktree_path),
        make_grep(worktree_path),
        make_list_dir(worktree_path),
    ]
