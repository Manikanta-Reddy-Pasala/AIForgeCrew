"""Misc agent tools + hallucination-tolerant aliases.

Groups the skill/workflow registries, code-quality tools (typecheck/run_tests/
lsp/format), OH-parity power tools (mcp/browse/ipython/delegate/github_pr/
multi_edit), server lifecycle (serve/stop_service), subtask status, the
Claude-Code/OpenHands meta no-ops (todo_write/glob/task), and the aliases that
delegate to the canonical file/shell/git/web tools so a model can use whichever
common name it pattern-matches to.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import os

from ._fs import file_patch, file_read, file_write, grep_repo, list_dir, run_shell
from ._repo import git_commit
from ._web import fetch_url


# ─── Hallucination-tolerant aliases ────────────────────────────────────
#
# Without these the Doer's "Tool 'read' not found" failure (observed in
# ONE-105) crashes the run. Each alias delegates to the canonical
# implementation so behaviour stays single-sourced; the model can use
# whichever common name it pattern-matches to without a redeploy.

def read(path: str) -> dict:
    """Alias for :func:`file_read`."""
    return file_read(path)


def write(path: str, content: str) -> dict:
    """Alias for :func:`file_write`."""
    return file_write(path, content)


def patch(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch`."""
    return file_patch(path, old_text, new_text)


def edit(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch`.

    Local models (Qwen3-Coder) consistently emit a tool call named
    ``edit`` — the Claude/aider str-replace convention — which wasn't
    registered. ONE-7 spent 37 minutes looping "Tool 'edit' not found",
    never wrote a file, and blocked with ``no_changes``. Registering
    the alias closes that gap so the local-model Doer can actually
    mutate files."""
    return file_patch(path, old_text, new_text)


def str_replace(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch` (OpenHands str_replace_editor name)."""
    return file_patch(path, old_text, new_text)


def ls(path: str = "") -> dict:
    """Alias for :func:`list_dir`."""
    return list_dir(path)


def shell(cmd: str) -> dict:
    """Alias for :func:`run_shell`."""
    return run_shell(cmd)


def bash(cmd: str) -> dict:
    """Alias for :func:`run_shell`."""
    return run_shell(cmd)


def run(cmd: str) -> dict:
    """Alias for :func:`run_shell`.

    Local models routinely emit a bare ``run`` tool call ("Now run pytest…")
    which wasn't registered — the unknown-tool result then went un-appended,
    leaving an orphaned tool_call that 400'd the next request ("Missing tool
    results for tool_call_id") and aborted the node. Registering the alias
    keeps the ReAct history valid."""
    return run_shell(cmd)


def grep(pattern: str, path: str = ".") -> dict:
    """Alias for :func:`grep_repo`."""
    return grep_repo(pattern, path)


def search(pattern: str, path: str = ".") -> dict:
    """Alias for :func:`grep_repo`."""
    return grep_repo(pattern, path)


def http_get(url: str) -> dict:
    """Alias for :func:`fetch_url`."""
    return fetch_url(url)


def web_fetch(url: str) -> dict:
    """Alias for :func:`fetch_url`."""
    return fetch_url(url)


def subtask_update(slug: str, status: str) -> dict:
    """Flip ONE subtask's status as you work through the plan's subtickets:
    'running' when you start it, 'done' when its acceptance is met, 'failed' if
    blocked. The UI charts this live. ``slug`` is the subticket's slug from the
    plan. Status ∈ pending|running|done|failed|skipped."""
    try:
        ident = os.environ.get("AIFORGE_CURRENT_TICKET", "")
        if not ident:
            return {"ok": False, "error": "no current ticket in context"}
        from aiforge_core.tickets import store, subtasks
        t = store.get(ident)
        if t is None:
            return {"ok": False, "error": f"ticket not found: {ident}"}
        res = subtasks.update_subtask(t.id, slug, status, role="doer")
        # Push a LIVE event onto the chat stream so the pinned subtask dock
        # flips this row's status in real time (the store write alone only
        # shows up on reload). Best-effort — no session / no emitter = no-op.
        try:
            _sid = os.environ.get("AIFORGE_CURRENT_SESSION", "")
            if _sid:
                from aiforge_core.runtime import chat_approve
                chat_approve.emit(int(_sid),
                                  {"type": "subtask_update", "slug": slug,
                                   "status": status})
        except Exception:  # noqa: BLE001 — never break the tool on emit failure
            pass
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def serve(cmd: str, port: int = 0, wait_s: float = 12.0) -> dict:
    """Start a server/app in the BACKGROUND (returns pid + detected URL) so you
    can run the thing you built and hand back an endpoint. NOT for one-shot
    commands (use run_shell). Stop it with stop_service(pid)."""
    try:
        from aiforge_core.runtime.tools import serve as _serve
        return _serve.serve({"cmd": cmd, "port": port or None, "wait_s": wait_s})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def stop_service(pid: int) -> dict:
    """Stop a service started by serve(), by pid."""
    try:
        from aiforge_core.runtime.tools import serve as _serve
        return _serve.stop_service({"pid": pid})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def commit(message: str) -> dict:
    """Alias for :func:`git_commit`."""
    return git_commit(message)


def git_add_commit(message: str) -> dict:
    """Alias for :func:`git_commit`."""
    return git_commit(message)


# Claude-Code / OpenHands meta-tools local models (Qwen3-Coder) emit
# from training but which have no side effect in our pipeline. ADK
# hard-errors on an unregistered function name and that BLOCKS the whole
# ticket (ONE-7: 'Tool todo_write not found' → no_changes → blocked),
# so we register them as no-ops that return success and nudge the model
# back toward the real tools. Cheaper than letting one stray call kill a
# 30-minute run.

def todo_write(_todos: str = "", **_kw) -> dict:
    """No-op planning scratchpad (Claude-Code TodoWrite). Accepted so a
    stray call doesn't abort the run; the plan already lives in state."""
    return {"ok": True, "note": "todo noted (no-op); use editor/bash to act"}


def todowrite(todos: str = "", **_kw) -> dict:
    """Alias spelling for :func:`todo_write`."""
    return todo_write(todos)


_GLOB_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", "target", ".next", ".idea", ".mypy_cache", ".pytest_cache"}


def _glob_walk(base, pat: str, bare: str, r) -> "tuple[list[str], bool]":
    """Walk ``base`` (pruning vcs/build noise in place) collecting repo-relative
    paths matching ``pat``/``bare`` by basename OR path. Returns
    ``(matches, capped)`` — capped at 500. In-place dir pruning means we never
    DESCEND into noise (a post-hoc filter still walked millions of entries and
    tested ABSOLUTE path parts, so a checkout under /venv/ skipped everything)."""
    import fnmatch
    import os as _os
    matches: list[str] = []
    for dirpath, dirs, files in _os.walk(base):
        dirs[:] = [d for d in dirs if d not in _GLOB_SKIP]
        for fn in files:
            rel = _os.path.relpath(_os.path.join(dirpath, fn), str(r))
            if fnmatch.fnmatch(fn, bare) or fnmatch.fnmatch(rel, pat) \
                    or fnmatch.fnmatch(rel, bare):
                matches.append(rel)
                if len(matches) >= 500:
                    return matches, True
    return matches, False


def glob(pattern: str = "*", path: str = ".") -> dict:
    """Find files by NAME pattern (Claude-Code Glob) — a real fnmatch walk, not a
    content grep. ``pattern`` matches the basename OR the repo-relative path (so
    ``**/*.py`` and ``*.py`` both work). Skips vcs/build noise."""
    from aiforge_core.runtime.sandbox import resolve_inside_root, root
    try:
        base = resolve_inside_root(path) if path not in ("", ".") else root()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    pat = (pattern or "*").strip()
    # Normalise a leading "**/" so "**/*.py" also matches root-level files
    # (fnmatch's * spans "/", so raw "**/*.py" REQUIRES a slash → misses
    # top-level files; str.lstrip("*/") over-strips to ".py").
    bare = pat[3:] if pat.startswith("**/") else pat
    matches, capped = _glob_walk(base, pat, bare, root())
    return {"ok": True, "pattern": pat, "count": len(matches),
            "truncated": capped, "matches": sorted(matches)}


def task(_description: str = "", **_kw) -> dict:
    """No-op for Claude-Code Task/Agent spawns — the Doer already runs
    inside the pipeline; sub-agent spawning goes through delegate_to_agent,
    not this name. Accepted so a stray call doesn't abort the run."""
    return {"ok": True, "note": "task no-op; use editor/bash directly"}


def skill_search(query: str, k: int = 5) -> dict:
    """Search the skill registry (SKILL.md playbooks) by relevance — find a
    reusable recipe before solving an unfamiliar problem from scratch."""
    try:
        from aiforge_core.runtime import skills as _skills
        return {"ok": True, "skills": _skills.search(query, None, k=int(k or 5))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def learn_skill(name: str, body: str, description: str = "",
                triggers: str = "", scope: str = "global") -> dict:
    """Author a reusable SKILL.md after solving a non-trivial, repeatable
    problem (the self-improving loop). Also recorded in knowledge memory.
    ``triggers`` is a comma-separated list of words that should surface it."""
    try:
        from aiforge_core.runtime import skills as _skills
        trig = [t.strip() for t in (triggers or "").split(",") if t.strip()]
        return _skills.write_skill(name=name, description=description, body=body,
                                   triggers=trig, cwd=None,
                                   scope=(scope or "global").lower())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def workflow_search(query: str, k: int = 5) -> dict:
    """Search the workflow registry (WORKFLOW.md end-to-end procedures) by
    relevance — find a reusable multi-step recipe before improvising one."""
    try:
        from aiforge_core.runtime import workflows as _workflows
        return {"ok": True, "workflows": _workflows.search(query, None, k=int(k or 5))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def learn_workflow(name: str, body: str, description: str = "",
                   triggers: str = "", scope: str = "global") -> dict:
    """Author a reusable WORKFLOW.md after running a repeatable end-to-end
    procedure, so future tickets reuse it. Also recorded in knowledge memory."""
    try:
        from aiforge_core.runtime import workflows as _workflows
        trig = [t.strip() for t in (triggers or "").split(",") if t.strip()]
        return _workflows.write_workflow(name=name, description=description,
                                         body=body, triggers=trig, cwd=None,
                                         scope=(scope or "global").lower())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ─── Code-quality + intelligence (parity with the chat surface) ─────────
# The team-mode Doer could edit + run shell, but couldn't type-check, run the
# test suite, query the language server, or auto-format — so it couldn't VERIFY
# its own work the way the chat agent can. Wire the same underlying tools in so
# a pipeline Doer can compile/test/format before it finishes.

def typecheck() -> dict:
    """Type-check the repo (mypy / tsc / …). Run after edits to catch type
    errors BEFORE finishing. No args."""
    from aiforge_core.runtime.tools.typecheck import typecheck as _tc
    return _tc()


def run_tests(mode: str = "fast", pattern: str = "") -> dict:
    """Run the repo's test suite. ``mode`` = fast|full; ``pattern`` filters by
    test name/path. Use it to prove a change works before finishing."""
    from aiforge_core.runtime.tools.test_runner import run_tests as _rt
    return _rt(mode=mode, pattern=pattern)


def lsp(command: str = "", path: str = "", line: int = 0,
        character: int = 0) -> dict:
    """Language-server query: ``command`` ∈ definition|references|hover|
    diagnostics|symbols at ``path``:``line``:``character``."""
    from aiforge_core.runtime.tools.lsp import lsp as _lsp
    return _lsp(command=command, path=path, line=line, character=character)


def format(path: str = ".") -> dict:
    """Auto-format code under ``path`` with the repo's formatter (black / prettier
    / gofmt / …). Idempotent; run before finishing so diffs stay clean."""
    from aiforge_core.runtime.tools.format import format as _fmt
    return _fmt(str(path or "."))


# ─── OH-parity power tools (MCP / browser / jupyter / delegate / PR / batch) ─
# These lived only on the chat surface; wiring them here gives the team-mode
# Doer the same reach: MCP tool servers, a headless browser, a persistent
# Jupyter kernel, sub-agent delegation, GitHub PRs, and atomic multi-file edits.

def mcp(command: str, endpoint: str = "", tool: str = "",
        arguments: "dict | None" = None) -> dict:
    """MCP bridge. ``command`` ∈ list_endpoints | list_tools | call_tool. For
    call_tool pass ``endpoint`` + ``tool`` + ``arguments``."""
    try:
        from aiforge_core.runtime.tools.mcp_client import mcp as _mcp
        return _mcp(command, endpoint=endpoint or None, tool=tool or None,
                    arguments=arguments)
    except Exception as exc:  # noqa: BLE001 — soft-fail
        return {"ok": False, "error": str(exc)}


def browse(command: str, url: str = "", selector: str = "", text: str = "",
           path: str = "") -> dict:
    """Headless browser. ``command`` ∈ goto|screenshot|click|fill|extract_text|
    close. Pass ``url`` for goto, ``selector`` (+``text``) for click/fill."""
    try:
        from aiforge_core.runtime.tools.browser import browse as _browse
        return _browse(command, url=url or None, selector=selector or None,
                       text=text or None, path=path or None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def execute_ipython_cell(code: str, timeout: int = 0) -> dict:
    """Run Python in a persistent IPython kernel (state persists across calls).
    Approval-gated — arbitrary code execution."""
    try:
        from aiforge_core.runtime.tools.ipython_kernel import (
            execute_ipython_cell as _ex)
        return _ex(code, **({"timeout": timeout} if timeout else {}))
    except Exception as exc:  # noqa: BLE001 — jupyter_client may be absent
        return {"ok": False, "error": str(exc)}


def delegate_to_agent(role: str, prompt: str, timeout: int = 600) -> dict:
    """Hand a focused sub-task to another agent role (its own ADK runner) and
    block for the result."""
    try:
        from aiforge_core.runtime.tools.delegation import delegate_to_agent as _d
        return _d(role, prompt, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def github_pr(title: str, body: str = "", base: str = "main", head: str = "",
              draft: bool = False) -> dict:
    """Open a GitHub pull request from the current branch via the ``gh`` CLI
    (must be installed + authenticated). Approval-gated."""
    import shutil
    import subprocess
    from ..sandbox import root
    if not title:
        return {"ok": False, "error": "missing 'title'"}
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh_not_installed",
                "hint": "install the GitHub CLI (gh) + `gh auth login`"}
    cmd = ["gh", "pr", "create", "--title", title, "--body", body or "",
           "--base", base or "main"]
    if head:
        cmd += ["--head", head]
    if draft:
        cmd += ["--draft"]
    try:
        r = subprocess.run(cmd, cwd=str(root()), capture_output=True,
                           text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False,
                    "error": (r.stderr or r.stdout or "").strip()[:400]}
        return {"ok": True, "url": (r.stdout or "").strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def multi_edit(edits: list) -> dict:
    """Apply a BATCH of edits in ONE call. Each edit = ``{path, old_text,
    new_text}`` (first-match replace, file_patch semantics). Returns a per-edit
    result list; ``ok`` is True only when every edit applied."""
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list of "
                "{path, old_text, new_text}"}
    results = []
    ok_all = True
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            results.append({"i": i, "ok": False, "error": "not an object"})
            ok_all = False
            continue
        r = file_patch(str(e.get("path") or ""),
                       str(e.get("old_text") or e.get("old_str") or ""),
                       str(e.get("new_text") or e.get("new_str") or ""))
        results.append({"i": i, "path": e.get("path"), **r})
        ok_all = ok_all and bool(r.get("ok"))
    return {"ok": ok_all, "results": results}
