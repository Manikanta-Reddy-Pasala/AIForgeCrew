"""smolagents Tool wrappers for the Doer agent.

Tools are created as callables via the smolagents ``@tool`` decorator or
as :class:`~smolagents.Tool` subclasses.  All write tools are scope-checked
via an injected :class:`~aiforge_core.doer.scope_guard.ScopeGuard`.

Factories (``make_*``) close over ``worktree_path`` and ``scope_guard`` so
each agent invocation gets its own correctly-bound copies.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

from smolagents import tool

from .scope_guard import ScopeGuard, ScopeViolation


# Hard caps applied to every tool result before returning to the LLM.
# Lifted from GenericAgent's _shrink_code idea: long fenced blocks in tool
# output (compile dumps with embedded code, apply_implementation reports
# echoing find/replace, grep hits in heavily-commented files) push prompt
# tokens up by ~20-30% on multi-step tickets. Shrinking the body of
# fenced ``` ``` blocks past 6 lines to a 5-line preview keeps the
# semantic signal (which file / which symbol) without the boilerplate.

_FENCED_BLOCK_RX = re.compile(r"```[\s\S]*?```")


def _shrink_fenced_blocks(text: str, preview_lines: int = 5) -> str:
    """Replace bodies of fenced code blocks longer than 6 lines with a
    short preview + line count. No-op on already-short blocks. Leaves the
    fence language tag intact so the model still sees what kind of code it
    was looking at.
    """
    def _shrink(m: re.Match) -> str:
        block = m.group(0)
        lines = block.split("\n")
        if len(lines) < 3:
            return block
        lang = lines[0].replace("```", "").strip()
        body = lines[1:-1]
        if len(body) <= 6:
            return block
        head = "\n".join(body[:preview_lines])
        return f"```{lang}\n{head}\n  ... ({len(body)} lines)\n```"
    return _FENCED_BLOCK_RX.sub(_shrink, text)


def _cap_lines(text: str, max_lines: int, hint: str = "") -> str:
    """Return *text* unchanged if ≤ max_lines; otherwise keep the first
    max_lines and append a truncation marker. Optional *hint* tells the
    model what to do next (e.g. "use offset/limit").
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = "\n".join(lines[:max_lines])
    suffix = f"\n…[truncated, {len(lines) - max_lines} more lines."
    if hint:
        suffix += f" {hint}"
    suffix += "]"
    return kept + suffix


def _repo_name_for_worktree(worktree_path: str) -> str:
    """Extract the repo directory name that backs a worktree path.

    Worktrees live at ``<repo>/.aiforge-worktrees/<ticket>``; the directory
    just above ``.aiforge-worktrees`` is the repo name. Returns empty when
    the path doesn't follow that layout.
    """
    parts = os.path.abspath(worktree_path).split(os.sep)
    if ".aiforge-worktrees" in parts:
        idx = parts.index(".aiforge-worktrees")
        if idx >= 1:
            return parts[idx - 1]
    return ""


def _strip_repo_prefix(path: str, repo_name: str) -> str:
    """Drop a leading ``<repo_name>/`` from *path* if present.

    Planner-written ``## Files`` sections use repo-qualified paths like
    ``TallyConnector/README.md`` but the worktree root IS the repo, so the
    literal form double-joins. Normalize before resolving.
    """
    if not repo_name:
        return path
    prefix = repo_name + "/"
    if not os.path.isabs(path) and path.startswith(prefix):
        return path[len(prefix):]
    return path


# ─────────────────────────── read_file ──────────────────────────────────

def make_read_file(worktree_path: str) -> Callable:
    """Return a ``read_file`` tool bound to *worktree_path*.

    Includes:
      * ``offset`` / ``limit`` line-range args for targeted reads.
      * A per-tick read cache: re-reading the same file (with the same
        range) returns a short stub instead of re-dumping the body. This
        is the biggest single ctx win — empirically 3× re-reads of a
        387-line Java file added ~60KB per step (ONE-51 step 19 had 612k
        input tokens; ~30% was redundant file content).
    """

    repo_name = _repo_name_for_worktree(worktree_path)
    # Cache: key = (resolved_path, offset, limit) → step_seq
    seen: dict[tuple[str, int, int], int] = {}

    @tool
    def read_file(
        path: str,
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        """Read a UTF-8 file. Re-reads return a stub (saves context).

        Args:
            path: File path (absolute or relative to the worktree root).
            offset: 1-based starting line. ``0`` means start of file.
            limit: Max lines to return. ``0`` means whole file.
        """
        path = _strip_repo_prefix(path, repo_name)
        resolved = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                full = fh.read()
        except FileNotFoundError:
            return f"ERROR: file not found: {resolved}"
        except OSError as exc:
            return f"ERROR: {exc}"

        if offset > 0 or limit > 0:
            lines = full.splitlines()
            start = max(0, offset - 1) if offset > 0 else 0
            end = start + limit if limit > 0 else len(lines)
            sliced = lines[start:end]
            content = "\n".join(sliced)
            header = (
                f"# {resolved} lines {start + 1}-{start + len(sliced)} "
                f"of {len(lines)}\n"
            )
        else:
            content = full
            header = ""

        key = (resolved, offset, limit)
        if key in seen:
            return (
                f"[file unchanged since previous read this tick — "
                f"{resolved}{' lines '+str(offset)+'-'+str(offset+limit-1) if limit else ''}. "
                f"Use offset/limit if you need a different slice, or "
                f"trust the prior observation.]"
            )
        seen[key] = len(seen) + 1
        # Hard caps: never return more than 60KB or 800 lines on first read.
        # Lines cap is the GA-style win — large Java files balloon from
        # commented sections we never need; force the agent to ask for a
        # slice via offset/limit rather than scrolling for free.
        body = (header + content)
        body = _cap_lines(body, max_lines=800,
                          hint="Pass offset/limit to read a specific slice.")
        if len(body) > 60000:
            body = body[:60000] + (
                f"\n…[truncated, file is {len(content)} chars total. "
                "Pass offset/limit to read a specific slice.]"
            )
        return body

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
    repo_name = _repo_name_for_worktree(worktree_path)

    @tool
    def edit_block(path: str, find: str, replace: str) -> str:
        """Replace an exact substring *find* with *replace* in a file.

        *find* must appear exactly once; errors on 0 or >1 matches.

        Args:
            path: File path (absolute or relative to the worktree root).
            find: Exact text to locate.  Must match uniquely.
            replace: Replacement text.
        """
        path = _strip_repo_prefix(path, repo_name)
        resolved = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            scope_guard.check(resolved)
        except ScopeViolation as exc:
            return f"SCOPE_VIOLATION: {exc}"
        # New-file creation: empty find + non-existing path writes *replace*
        # as the full content. Lets Doer emit a single edit_block for docs
        # tickets where the target file doesn't exist yet (was the gap on
        # ONE-45).
        if not os.path.exists(resolved):
            if find == "":
                os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
                with open(resolved, "w", encoding="utf-8") as fh:
                    fh.write(replace)
                counters["edit_block_ok"] = counters.get("edit_block_ok", 0) + 1
                return (f"OK: created {resolved} (+{len(replace)} chars, "
                        f"new file)")
            return (f"ERROR: file not found: {resolved} "
                    f"(tip: pass find=\"\" to create a new file)")
        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        count = src.count(find)
        if count == 0:
            # Structured miss hint — helps model self-correct instead of
            # blindly retrying slight variations. Uses difflib to find
            # near-matches in the file so the model sees the actual
            # whitespace / signature it should target.
            import difflib
            first_line = (find.splitlines() or [find])[0].strip()[:120]
            candidates: list[str] = []
            if first_line:
                matches = difflib.get_close_matches(
                    first_line, src.splitlines(), n=3, cutoff=0.55,
                )
                candidates = [m.strip()[:200] for m in matches]
            hint = ""
            if candidates:
                hint = (
                    "\nNear-matches actually in the file (copy verbatim, "
                    "preserve leading whitespace + tabs):\n  - "
                    + "\n  - ".join(candidates)
                )
            return (f"ERROR: find string not found in {resolved}. The file "
                    f"has {src.count(chr(10)) + 1} lines.{hint}")
        if count > 1:
            # Show each occurrence with a line-number breadcrumb so the
            # model can pick a unique surrounding context.
            lines = src.splitlines()
            hits: list[int] = []
            needle_head = find.splitlines()[0] if find.splitlines() else find
            for i, line in enumerate(lines, 1):
                if needle_head and needle_head in line:
                    hits.append(i)
                if len(hits) >= 5:
                    break
            loc = ", ".join(f"line {n}" for n in hits) or "multiple"
            return (f"ERROR: find string matches {count} times in {resolved} "
                    f"(at {loc}). Add surrounding context so the match is "
                    f"unique — include a preceding comment, imports block, "
                    f"or method signature.")
        new_src = src.replace(find, replace, 1)
        with open(resolved, "w", encoding="utf-8") as fh:
            fh.write(new_src)
        counters["edit_block_ok"] = counters.get("edit_block_ok", 0) + 1
        return f"OK: edited {resolved} (-{len(find)} +{len(replace)} chars)"

    return edit_block


# ─────────────────────────── write_file ─────────────────────────────────

def make_write_file(worktree_path: str, scope_guard: ScopeGuard,
                    counters: dict | None = None) -> Callable:
    """Return a ``write_file`` tool for creating new files from scratch.

    Doer's ``edit_block`` is in-place only; ``write_file`` handles the
    create-file path cleanly. Either tool bumps ``edit_block_ok`` so the
    harness's counter gate can accept a write-only ticket.
    """
    if counters is None:
        counters = {}
    repo_name = _repo_name_for_worktree(worktree_path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Create (or overwrite) a file with the given content.

        Use for NEW files — the ticket asks for a file that does not exist
        yet. For in-place edits to existing files, prefer ``edit_block``.

        Args:
            path: File path (absolute or relative to the worktree root).
            content: Full file contents to write.
        """
        path = _strip_repo_prefix(path, repo_name)
        resolved = (
            path if os.path.isabs(path)
            else os.path.join(worktree_path, path)
        )
        try:
            scope_guard.check(resolved)
        except ScopeViolation as exc:
            return f"SCOPE_VIOLATION: {exc}"
        is_new = not os.path.exists(resolved)
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as fh:
            fh.write(content)
        counters["edit_block_ok"] = counters.get("edit_block_ok", 0) + 1
        kind = "created" if is_new else "overwrote"
        return f"OK: {kind} {resolved} ({len(content)} chars)"

    return write_file


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
            counters.pop("last_compile_error", None)
        else:
            # Stash error so the orchestrator/feedback can surface it as a
            # fixlist hint in the next Doer tick. Clipped to 6KB so it
            # fits in a single chat turn.
            err_lines = [
                ln for ln in combined.splitlines()
                if "ERROR" in ln or "error:" in ln or "cannot find" in ln
            ][:30]
            counters["last_compile_error"] = "\n".join(err_lines)[:6000]
        return f"EXIT={proc.returncode}\n{tail}"

    return run_compile


# ─────────────────────────── grep ───────────────────────────────────────

def make_grep(worktree_path: str) -> Callable:
    """Return a ``grep`` tool bound to *worktree_path*."""

    repo_name = _repo_name_for_worktree(worktree_path)

    @tool
    def grep(pattern: str, path: str = "src/main/java") -> str:
        """Search for *pattern* (ripgrep regex) under *path* in the worktree.

        Args:
            pattern: Ripgrep regex pattern.
            path: Sub-path relative to the worktree root (default: src/main/java).
        """
        path = _strip_repo_prefix(path, repo_name)
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
        # Cap by line count too (GA-style) — heavily commented Java repos
        # produce 200+ matches on common identifiers; agent never needs
        # them all and they crowd the context.
        out = _cap_lines(out[:8000], max_lines=120,
                         hint="Narrow the pattern or scope to a sub-path.")
        return out

    return grep


# ─────────────────────────── list_dir ───────────────────────────────────

def make_list_dir(worktree_path: str) -> Callable:
    """Return a ``list_dir`` tool bound to *worktree_path*."""

    repo_name = _repo_name_for_worktree(worktree_path)

    @tool
    def list_dir(path: str = ".") -> str:
        """List directory contents relative to the worktree root.

        Args:
            path: Directory path (relative to worktree or absolute).
        """
        path = _strip_repo_prefix(path, repo_name)
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

    ``ticket_body_provider`` is unused now (kept for API stability). The Doer
    must emit edit_block calls itself — no implicit apply-from-body shortcut.
    """
    if counters is None:
        counters = {}
    _ = ticket_body_provider  # intentionally unused — Doer writes the code
    tools = [
        make_read_file(worktree_path),
        make_edit_block(worktree_path, scope_guard, counters),
        make_write_file(worktree_path, scope_guard, counters),
        make_run_compile(worktree_path, counters),
        make_grep(worktree_path),
        make_list_dir(worktree_path),
    ]
    # Opt-in: expose graph_rag MCP tools to the Doer when
    # AIFORGE_GRAPH_MCP_ENABLED=1. Doer can then call sym_lookup, impact,
    # cross_repo_flow, etc. for richer context.
    try:
        from aiforge_core.mcp_graph import graph_rag_tools
        _existing = {getattr(t, "name", None) for t in tools}
        for gt in graph_rag_tools():
            if getattr(gt, "name", None) in _existing:
                continue
            tools.append(gt)
    except Exception:
        pass
    # Opt-in web search — AIFORGE_WEB_SEARCH_ENABLED=1. Gives Doer a
    # fallback when the ticket references an external library/API spec
    # not found locally. Off by default (cost + distraction risk).
    if os.environ.get("AIFORGE_WEB_SEARCH_ENABLED", "0") == "1":
        try:
            from smolagents import DuckDuckGoSearchTool, VisitWebpageTool
            tools.extend([DuckDuckGoSearchTool(), VisitWebpageTool()])
        except Exception:
            pass
    return tools
