"""smolagents Tool factories for the Planner agent.

All tools close over a ``ctx`` dict with keys:
  ticket        — Ticket dataclass instance
  worktree_root — str path (default ~/codeRepo)
  store         — Store() instance (or None; instantiated lazily)
  log           — stdlib Logger

Factories follow the same pattern as aiforge_core.doer.tools.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

import psycopg
from smolagents import tool

# Imported at module level so tests can patch aiforge_core.planner.tools.tickets
from aiforge_core.runtime import tickets


# ─────────────────────────── _SIGNATURE_PATTERNS ────────────────────────

# Per-language regex patterns that match declaration lines.
# Each pattern must capture the full line (no groups required).
_JAVA_SIG_RE = re.compile(
    r"^\s*(?:public|protected|private)\s+"   # visibility
    r"(?:(?:static|final|abstract|synchronized|native|default|strictfp)\s+)*"  # modifiers
    r"(?:.+?\s+)?"                            # optional return type (non-greedy, any chars)
    r"\w[\w\d]*\s*\([^)]*\)"                 # method/constructor name + param list
    r"(?:\s*throws\s+[\w,\s]+)?"
    r"\s*\{?\s*$",
    re.MULTILINE,
)

_PYTHON_SIG_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+\w[\w\d_]*\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:\s*$",
    re.MULTILINE,
)

_TS_SIG_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+\w[\w\d]*|"
    r"(?:public|private|protected|static|readonly|\s)*\w[\w\d]*)\s*\([^)]*\)"
    r"(?:\s*:\s*[^\{]+)?\s*\{?\s*$",
    re.MULTILINE,
)


# ─────────────────────────── search_memory ──────────────────────────────

def make_search_memory(ctx: dict) -> Callable:
    """Return a ``search_memory`` tool that queries the hybrid memory store."""

    @tool
    def search_memory(query: str, role: str = "planner", top_k: int = 10) -> str:
        """Search the memory store (BM25 + vector + rerank) for relevant facts.

        Args:
            query: Natural-language search query.
            role: Memory policy role — 'planner', 'doer', or 'learner'.
            top_k: Maximum number of results to return.
        """
        try:
            from aiforge_core.rag.retriever import retrieve_for_role_li

            ticket = ctx["ticket"]
            parent_id = getattr(ticket, "parent_id", None)
            hits = retrieve_for_role_li(None, role, query, parent_id=parent_id)
            if not hits:
                return "(no memory results)"
            lines = []
            for h in hits[:top_k]:
                text_preview = (h.text or "")[:200].replace("\n", " ")
                lines.append(f"{h.id} | {h.tier} | {h.score:.3f} | {text_preview}")
            return "\n".join(lines)
        except Exception as exc:
            return f"ERROR: search_memory failed: {exc}"

    return search_memory


# ─────────────────────────── grep_repos ─────────────────────────────────

def make_grep_repos(ctx: dict) -> Callable:
    """Return a ``grep_repos`` tool that searches across all repos under WORKTREE_ROOT."""

    @tool
    def grep_repos(pattern: str, glob: str = "*.java,*.py,*.ts,*.tsx") -> str:
        """Search for *pattern* (ripgrep regex) across all repos under WORKTREE_ROOT.

        Args:
            pattern: Ripgrep regex pattern.
            glob: Comma-separated glob patterns (e.g. '*.java,*.py').
        """
        root = ctx.get("worktree_root", os.path.expanduser("~/codeRepo"))
        globs = [g.strip() for g in glob.split(",") if g.strip()]

        cmd = ["rg", "-nI", "--max-count", "50", "--color=never"]
        for g in globs:
            cmd += ["--glob", g]
        cmd.append(pattern)
        cmd.append(root)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return "ERROR: rg (ripgrep) not installed"
        except subprocess.TimeoutExpired:
            return "ERROR: grep_repos timed out after 30s"

        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode == 1 and not out.strip():
            return "(no matches)"
        if proc.returncode not in (0, 1):
            err = proc.stderr.decode("utf-8", "replace")[:200]
            return f"ERROR: rg exit={proc.returncode}: {err}"
        return out[:8000]

    return grep_repos


# ─────────────────────────── list_repos ─────────────────────────────────

def make_list_repos(ctx: dict) -> Callable:
    """Return a ``list_repos`` tool listing directories under WORKTREE_ROOT."""

    @tool
    def list_repos() -> str:
        """List all repository directories under WORKTREE_ROOT (one per line)."""
        root = ctx.get("worktree_root", os.path.expanduser("~/codeRepo"))
        try:
            entries = sorted(
                e for e in os.listdir(root)
                if os.path.isdir(os.path.join(root, e)) and not e.startswith(".")
            )
        except FileNotFoundError:
            return f"ERROR: WORKTREE_ROOT not found: {root}"
        except OSError as exc:
            return f"ERROR: {exc}"
        return "\n".join(entries) if entries else "(empty)"

    return list_repos


# ─────────────────────────── read_file ──────────────────────────────────

def make_read_file(ctx: dict) -> Callable:
    """Return a ``read_file`` tool; resolves relative paths against WORKTREE_ROOT."""

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 400) -> str:
        """Read a slice of a UTF-8 file.

        Args:
            path: Absolute path or path relative to WORKTREE_ROOT.
            start_line: First line to return (1-indexed).
            end_line: Last line to return (inclusive).
        """
        root = ctx.get("worktree_root", os.path.expanduser("~/codeRepo"))
        resolved = path if os.path.isabs(path) else os.path.join(root, path)
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return f"ERROR: file not found: {resolved}"
        except OSError as exc:
            return f"ERROR: {exc}"

        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        return "".join(lines[start_idx:end_idx])

    return read_file


# ─────────────────────────── list_dir ───────────────────────────────────

def make_list_dir(ctx: dict) -> Callable:
    """Return a ``list_dir`` tool that lists a directory relative to WORKTREE_ROOT."""

    @tool
    def list_dir(path: str = ".") -> str:
        """List directory contents (relative to WORKTREE_ROOT or absolute).

        Args:
            path: Directory path.
        """
        root = ctx.get("worktree_root", os.path.expanduser("~/codeRepo"))
        target = path if os.path.isabs(path) else os.path.join(root, path)
        try:
            entries = sorted(os.listdir(target))
        except FileNotFoundError:
            return f"ERROR: directory not found: {target}"
        except NotADirectoryError:
            return f"ERROR: not a directory: {target}"
        return "\n".join(entries) if entries else "(empty)"

    return list_dir


# ─────────────────────────── extract_signatures ─────────────────────────

def make_extract_signatures(ctx: dict) -> Callable:
    """Return an ``extract_signatures`` tool that extracts method/class signatures."""

    @tool
    def extract_signatures(path: str, start_line: int = 1, end_line: int = 600) -> str:
        """Extract public method/class signatures from a Java/Python/TS file.

        Scans only the requested line range and returns one signature per line,
        prefixed with its 1-indexed line number.  Example output::

            82: public <T> Mono<ResponseEntity<?>> queryAndProcess(@RequestBody MessageRequest<T> request)
            149: public Mono<Object> processMessageDirect(MessageRequest<?> request)

        Args:
            path: Repo-relative or absolute path to the source file.
            start_line: First line to scan (1-indexed, inclusive).
            end_line: Last line to scan (inclusive).  Defaults to 600.
        """
        root = ctx.get("worktree_root", os.path.expanduser("~/codeRepo"))
        resolved = path if os.path.isabs(path) else os.path.join(root, path)
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
        except FileNotFoundError:
            return f"ERROR: file not found: {resolved}"
        except OSError as exc:
            return f"ERROR: {exc}"

        # Determine which regex to use based on file extension.
        ext = os.path.splitext(resolved)[1].lower()
        if ext == ".java":
            pattern = _JAVA_SIG_RE
        elif ext == ".py":
            pattern = _PYTHON_SIG_RE
        elif ext in (".ts", ".tsx", ".js", ".jsx"):
            pattern = _TS_SIG_RE
        else:
            # Best-effort: try all patterns.
            pattern = re.compile(
                r"(?:"
                + _JAVA_SIG_RE.pattern
                + r"|" + _PYTHON_SIG_RE.pattern
                + r"|" + _TS_SIG_RE.pattern
                + r")",
                re.MULTILINE,
            )

        start_idx = max(0, start_line - 1)
        end_idx = min(len(all_lines), end_line)
        slice_lines = all_lines[start_idx:end_idx]

        results: list[str] = []
        for relative_idx, line in enumerate(slice_lines):
            lineno = start_idx + relative_idx + 1  # 1-indexed absolute
            stripped = line.rstrip("\n")
            if pattern.match(stripped):
                results.append(f"{lineno}: {stripped.strip()}")

        if not results:
            return "(no signatures found in range)"

        output = "\n".join(results)
        return output[:4000]

    return extract_signatures


# ─────────────────────────── write_plan ─────────────────────────────────

def make_write_plan(ctx: dict) -> Callable:
    """Return a ``write_plan`` tool that enriches the ticket body in Postgres."""

    @tool
    def write_plan(
        files: list,
        plan: str,
        signatures: str = "",
        pitfalls: str = "",
        cross_service: str = "",
    ) -> str:
        """Append ## Files, ## Plan, ## Signatures, ## Compile pitfalls, ## Cross-service.

        Mutates the ticket body in Postgres via a direct UPDATE.  Emits a
        planner.plan.written log event.

        Args:
            files: List of file paths that the Doer will need to edit.
            plan: Numbered high-level plan (plain text). High-level only — the Doer
                writes the actual code.
            signatures: Optional block of verified method signatures (one per line
                with file:line prefix). Lets the Doer call real methods, not
                invented ones.
            pitfalls: Optional compile pitfalls pulled from memory
                (e.g. "ResponseEntity<?> cast required when lambda branches
                return different payload types").
            cross_service: Optional cross-service coordination notes.
        """
        try:
            from aiforge_core.runtime.config import AIFORGE_DSN

            ticket = ctx["ticket"]
            current_body = getattr(ticket, "body", "") or ""

            files_block = "\n## Files\n" + "".join(f"- {f}\n" for f in files)
            plan_block = f"\n## Plan\n{plan}\n"
            sig_block = f"\n## Signatures\n{signatures}\n" if signatures else ""
            pit_block = f"\n## Compile pitfalls\n{pitfalls}\n" if pitfalls else ""
            cross_block = f"\n## Cross-service\n{cross_service}\n" if cross_service else ""

            new_body = current_body + files_block + plan_block + sig_block + pit_block + cross_block

            with psycopg.connect(AIFORGE_DSN, autocommit=False,
                                 connect_timeout=5,
                                 options="-c statement_timeout=15000") as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tickets SET body = %s WHERE id = %s",
                        (new_body, ticket.id),
                    )
                conn.commit()

            # Reflect change back on the in-memory ticket so callers see it.
            ticket.body = new_body

            log = ctx.get("log")
            if log is not None:
                from aiforge_core.runtime.logging_setup import emit
                emit(log, "planner.plan.written",
                     ticket=getattr(ticket, "identifier", "?"),
                     files=files,
                     plan_len=len(plan))

            return f"OK: plan written ({len(files)} files, {len(plan)} chars)"
        except Exception as exc:
            return f"ERROR: write_plan failed: {exc}"

    return write_plan


# ─────────────────────────── create_child_ticket ────────────────────────

def make_create_child_ticket(ctx: dict) -> Callable:
    """Return a ``create_child_ticket`` tool that fans out sub-tickets."""

    @tool
    def create_child_ticket(
        title: str,
        body: str,
        project: str,
        assignee_role: str = "planner",
    ) -> str:
        """Create a child ticket under the current ticket.

        Args:
            title: Short title for the child ticket.
            body: Detailed body / spec for the child ticket.
            project: Project key (e.g. 'PosServerBackend').
            assignee_role: Role that should process the child ticket.
        """
        try:
            ticket = ctx["ticket"]
            child = tickets.create(
                title=title,
                body=body,
                parent_id=ticket.id,
                project=project,
                assignee_role=assignee_role,
            )
            return child.identifier
        except Exception as exc:
            return f"ERROR: create_child_ticket failed: {exc}"

    return create_child_ticket


# ─────────────────────────── factory ────────────────────────────────────

def make_tools(ctx: dict) -> list:
    """Build the tool list for a Planner agent invocation.

    ``final_answer`` is excluded: ``ToolCallingAgent`` adds its own
    ``FinalAnswerTool`` automatically.
    """
    tools = [
        make_search_memory(ctx),
        make_grep_repos(ctx),
        make_list_repos(ctx),
        make_read_file(ctx),
        make_list_dir(ctx),
        make_extract_signatures(ctx),
        make_write_plan(ctx),
        make_create_child_ticket(ctx),
    ]
    # Opt-in graph_rag MCP tools (AIFORGE_GRAPH_MCP_ENABLED=1). Planner gets
    # sym_lookup / impact / cross_repo_flow / ticket_brief etc. so it can
    # build plans from the Neo4j graph instead of grep alone.
    try:
        from aiforge_core.mcp_graph import graph_rag_tools
        tools.extend(graph_rag_tools())
    except Exception:
        pass
    return tools
