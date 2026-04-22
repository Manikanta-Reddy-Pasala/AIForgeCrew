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

def make_edit_block(worktree_path: str, scope_guard: ScopeGuard,
                    counters: dict | None = None) -> Callable:
    """Return an ``edit_block`` tool bound to *worktree_path* and *scope_guard*.

    On each successful edit, bumps ``counters['edit_block_ok']`` so the bridge
    can verify the agent did real work before accepting final_answer.
    """
    if counters is None:
        counters = {}

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
        counters["edit_block_ok"] = counters.get("edit_block_ok", 0) + 1
        return f"OK: edited {resolved} (-{len(find)} +{len(replace)} chars)"

    return edit_block


# ─────────────────────────── run_compile ────────────────────────────────

def make_run_compile(worktree_path: str, counters: dict | None = None) -> Callable:
    """Return a ``run_compile`` tool bound to *worktree_path*.

    On each compile that returns EXIT=0, bumps ``counters['compile_green']``.
    """
    if counters is None:
        counters = {}

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
        if proc.returncode == 0:
            counters["compile_green"] = counters.get("compile_green", 0) + 1
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


# ─────────────────────────── apply_implementation ──────────────────────

def make_apply_implementation(worktree_path: str, scope_guard: ScopeGuard,
                              counters: dict | None = None,
                              ticket_body_provider: Callable[[], str] | None = None) -> Callable:
    """Return an ``apply_implementation`` tool that parses the ticket's
    ``## Implementation`` section and applies all find/replace blocks.

    This lets the Doer execute planner-prepared edits without having to
    emit long strings via JSON tool calls — which qwen-coder via smolagents
    routinely refuses to do. Each successful block bumps
    ``counters['edit_block_ok']``.

    The ticket body is fetched lazily via ``ticket_body_provider`` so fresh
    planner-written sections are picked up on retry.
    """
    if counters is None:
        counters = {}

    import re as _re

    def _normalize_newlines(s: str) -> str:
        # psql heredoc inserts sometimes store literal '\n' escapes.
        if "\\n" in s and "\n" not in s:
            s = s.replace("\\n", "\n")
        return s

    def _parse_impl_blocks(body: str) -> list[dict]:
        body = _normalize_newlines(body)
        # Find the ## Implementation section.
        m = _re.search(r"##\s*Implementation\s*\n", body, _re.IGNORECASE)
        if not m:
            return []
        section = body[m.end():]
        # Stop at next ## heading.
        next_hdr = _re.search(r"\n##\s", section)
        if next_hdr:
            section = section[:next_hdr.start()]
        # Each block: ### <path>\n```\nfind:\n<x>\n---\nreplace:\n<y>\n```
        blocks: list[dict] = []
        for m2 in _re.finditer(
            r"###\s+([^\n]+?)\s*\n+```[a-zA-Z]*\n+find:\n(.*?)\n---\n+replace:\n(.*?)\n+```",
            section, _re.DOTALL,
        ):
            blocks.append({
                "path": m2.group(1).strip(),
                "find": m2.group(2),
                "replace": m2.group(3),
            })
        return blocks

    def _strip_repo_prefix(p: str) -> str:
        # Planner writes "PosClientBackend/src/..." but worktree is rooted
        # at the project. Drop leading "<ProjectName>/" if present.
        if "/" in p and not os.path.isabs(p):
            parts = p.split("/", 1)
            abs_candidate = os.path.join(worktree_path, parts[1])
            if os.path.exists(abs_candidate):
                return parts[1]
        return p

    @tool
    def apply_implementation() -> str:
        """Apply every find/replace block from the ticket's ## Implementation section.

        No arguments. Parses the current ticket body, finds ``### <path>`` + fenced
        ``find:`` / ``replace:`` pairs, and writes each change to disk. Scope guard
        enforced. Returns a summary line per block (OK / ERROR / SCOPE_VIOLATION).
        Use this as step 1 when the ticket has a ## Implementation section — it's
        more reliable than calling edit_block manually.

        Args:
        """
        body = ""
        if ticket_body_provider is not None:
            try:
                body = ticket_body_provider() or ""
            except Exception as exc:  # pragma: no cover
                return f"ERROR: failed to load ticket body: {exc}"
        blocks = _parse_impl_blocks(body)
        if not blocks:
            return "ERROR: no ## Implementation section found in ticket body"
        results: list[str] = []
        for i, blk in enumerate(blocks, 1):
            path = _strip_repo_prefix(blk["path"])
            resolved = (
                path if os.path.isabs(path)
                else os.path.join(worktree_path, path)
            )
            try:
                scope_guard.check(resolved)
            except ScopeViolation as exc:
                results.append(f"{i}) SCOPE_VIOLATION: {path}: {exc}")
                continue
            if not os.path.exists(resolved):
                results.append(f"{i}) ERROR: file not found: {path}")
                continue
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            find = blk["find"]
            count = src.count(find)
            if count == 0:
                results.append(f"{i}) ERROR: find not found in {path}")
                continue
            if count > 1:
                results.append(f"{i}) ERROR: find matches {count}x in {path}")
                continue
            new_src = src.replace(find, blk["replace"], 1)
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write(new_src)
            counters["edit_block_ok"] = counters.get("edit_block_ok", 0) + 1
            results.append(f"{i}) OK: edited {path} "
                           f"(-{len(find)} +{len(blk['replace'])} chars)")
        return "\n".join(results)

    return apply_implementation


# ─────────────────────────── final_answer ───────────────────────────────

@tool
def final_answer(summary: str) -> str:
    """Signal successful completion.  Returns the summary verbatim.

    Args:
        summary: Human-readable description of what was done.
    """
    return summary


# ─────────────────────────── factory ────────────────────────────────────

def make_tools(worktree_path: str, scope_guard: ScopeGuard,
               counters: dict | None = None,
               ticket_body_provider: Callable[[], str] | None = None) -> list:
    """Build the tool list for a Doer agent invocation.

    ``counters`` is a dict that edit_block and run_compile bump so the caller
    can verify real work happened (edit_block_ok > 0 AND compile_green > 0)
    before accepting final_answer.

    ``final_answer`` is intentionally excluded: ``ToolCallingAgent`` adds its
    own ``FinalAnswerTool`` automatically, so adding a second one would
    cause a duplicate-name error at agent construction time.
    """
    if counters is None:
        counters = {}
    return [
        make_read_file(worktree_path),
        make_apply_implementation(worktree_path, scope_guard, counters,
                                  ticket_body_provider=ticket_body_provider),
        make_edit_block(worktree_path, scope_guard, counters),
        make_run_compile(worktree_path, counters),
        make_grep(worktree_path),
        make_list_dir(worktree_path),
    ]
