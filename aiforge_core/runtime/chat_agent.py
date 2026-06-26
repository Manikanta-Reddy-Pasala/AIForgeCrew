"""Conversational full-filesystem coding agent (deploy-anywhere chat).

A lightweight, provider-agnostic ReAct loop — NOT the ticket pipeline.
Streams steps back to the Chat UI. The model talks a plain text
protocol (no native tool-calling) so it works across every backend the
home page can point at (LM Studio, OpenRouter, Groq, vLLM, cloud).

Tools run with TOTAL filesystem + exec freedom by design (the operator
chose whole-machine access). An optional ``AIFORGE_WORKSPACE_DIR``
clamps file/exec operations to a root for cautious deploys.

Protocol — each model turn must be either a tool call:

    THOUGHT: <reasoning>
    ACTION: <tool_name>
    ARGS_JSON: {"path": "..."}

or a final answer:

    THOUGHT: <reasoning>
    FINAL: <message to the user>

Public surface:
    run_chat_agent(messages, *, cwd, role, max_steps, complete_fn)
        -> Iterator[dict]   # SSE-ready event dicts
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

_ACTION_RE = re.compile(r"ACTION:\s*([A-Za-z_]+)", re.IGNORECASE)
_ARGS_RE = re.compile(r"ARGS_JSON:\s*(\{.*\})", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"FINAL:\s*(.*)", re.IGNORECASE | re.DOTALL)
_ASK_RE = re.compile(r"ASK:\s*(.*)", re.IGNORECASE | re.DOTALL)
_THOUGHT_RE = re.compile(r"THOUGHT:\s*(.*?)(?:\n[A-Z_]+:|$)", re.IGNORECASE | re.DOTALL)

_MAX_OBS = 6000  # truncate tool output fed back to the model


def _workspace_root() -> Path | None:
    raw = os.environ.get("AIFORGE_WORKSPACE_DIR")
    return Path(os.path.expanduser(raw)).resolve() if raw else None


def _resolve(cwd: str, path: str) -> Path:
    """Resolve ``path`` against the session cwd. When AIFORGE_WORKSPACE_DIR
    is set, reject anything that escapes it; otherwise total freedom."""
    base = Path(cwd).expanduser().resolve()
    p = (base / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    root = _workspace_root()
    if root is not None and root not in p.parents and p != root:
        raise PermissionError(f"path escapes AIFORGE_WORKSPACE_DIR: {path}")
    return p


# ─────────────────────────── tools ──────────────────────────────────

def _t_file_read(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {args['path']}"}
    return {"ok": True, "content": p.read_text(encoding="utf-8", errors="replace")}


def _t_file_write(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.get("content", ""), encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(args.get("content", ""))}


def _t_file_patch(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    if not p.is_file():
        return {"ok": False, "error": "not_found"}
    body = p.read_text(encoding="utf-8")
    old = args["old_text"]
    n = body.count(old)
    if n == 0:
        return {"ok": False, "error": "old_text_not_found"}
    if n > 1:
        return {"ok": False, "error": "ambiguous_match", "occurrences": n}
    p.write_text(body.replace(old, args["new_text"], 1), encoding="utf-8")
    return {"ok": True, "path": str(p)}


def _t_list_dir(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args.get("path", "."))
    if not p.is_dir():
        return {"ok": False, "error": f"not a dir: {args.get('path')}"}
    entries = [
        (c.name + "/") if c.is_dir() else c.name
        for c in sorted(p.iterdir())
    ]
    return {"ok": True, "entries": entries}


def _t_run_command(args: dict, cwd: str) -> dict:
    cmd = args["cmd"]
    from aiforge_core.runtime.tools import delete_guard
    allow_delete = delete_guard.allow_delete(
        ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
    if not allow_delete and not args.get("confirm_delete") \
            and delete_guard.is_destructive_delete(cmd):
        return {"ok": False, "blocked": "delete",
                "error": delete_guard.REFUSAL + " (re-issue with "
                         "confirm_delete=true after the user agrees.)"}
    base = cwd
    root = _workspace_root()
    if root is not None:
        base = str(root)
    # Default generous so dependency installs / builds (npm ci, mvn package,
    # pip install) aren't killed mid-run; agent may override per call.
    default_to = int(os.environ.get("AIFORGE_CHAT_CMD_TIMEOUT_S", "600"))
    timeout = int(args.get("timeout", default_to))
    # Run in its own process group so the Stop button can kill the whole
    # tree (the shell + its children), and poll for cancellation.
    import time as _time

    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=base, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if sid is not None:
        try:
            chat_cancel.track_pgid(sid, os.getpgid(proc.pid))
        except Exception:  # noqa: BLE001
            pass
    deadline = _time.monotonic() + timeout
    while proc.poll() is None:
        if sid is not None and chat_cancel.is_cancelled(sid):
            _kill_proc(proc)
            return {"ok": False, "stopped": True, "error": "stopped by user"}
        if _time.monotonic() > deadline:
            _kill_proc(proc)
            return {"ok": False, "error": f"timeout after {timeout}s "
                    "(pass a larger \"timeout\" arg for long builds)"}
        _time.sleep(0.2)
    out, err = proc.communicate()
    return {"ok": proc.returncode == 0, "code": proc.returncode,
            "stdout": (out or "")[-_MAX_OBS:], "stderr": (err or "")[-_MAX_OBS:]}


def _kill_proc(proc) -> None:
    import signal as _sig
    for s in (_sig.SIGTERM, _sig.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), s)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    # Reap so the killed child's pipe FDs are freed (no zombie leak).
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _t_memory_lookup(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.memory import unified_query as _uq
        res = _uq.query(args["query"], limit=int(args.get("limit", 6)))
        return {"ok": True, "hits": [
            {"text": (h.get("text") or "")[:400], "source": h.get("source")}
            for h in res.get("hits", [])
        ]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_memory_write(args: dict, cwd: str) -> dict:
    """Persist a durable fact/decision into the knowledge memory so future
    chats + tickets recall it. repo defaults to the working dir's name."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        repo = args.get("repo") or os.path.basename(os.path.normpath(cwd)) or "chat"
        return _mw(
            text=args["text"],
            kind=args.get("kind", "note"),
            tags=list(args.get("tags") or []) + ["chat"],
            decision=bool(args.get("decision")),
            repo=repo,
        )
    except KeyError:
        return {"ok": False, "error": "missing arg: text"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", ".next", "target", ".gradle", ".idea"}


def _t_find(args: dict, cwd: str) -> dict:
    """Fuzzy-locate files/dirs by partial name — so a vague/wrong folder
    name still resolves. args: name (substring, case-insensitive),
    kind ('dir'|'file'|'any'), limit."""
    base = str(_workspace_root() or cwd)
    needle = (args.get("name") or args.get("query") or "").lower()
    kind = (args.get("kind") or "any").lower()
    limit = int(args.get("limit", 60))
    hits: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if kind in ("dir", "any"):
            for d in dirs:
                if not needle or needle in d.lower():
                    hits.append(os.path.relpath(os.path.join(root, d), base) + "/")
        if kind in ("file", "any"):
            for f in files:
                if not needle or needle in f.lower():
                    hits.append(os.path.relpath(os.path.join(root, f), base))
        if len(hits) >= limit:
            break
    return {"ok": True, "base": base, "matches": hits[:limit],
            "truncated": len(hits) > limit}


def _t_grep(args: dict, cwd: str) -> dict:
    """Recursive content search (ripgrep if present, else Python). Tolerant
    of a wrong ``path``: falls back to the working dir + says so. args:
    pattern (required), path (optional), glob (optional file filter)."""
    import re as _re2
    import shutil as _sh
    pattern = args.get("pattern") or args.get("query") or ""
    if not pattern:
        return {"ok": False, "error": "missing 'pattern'"}
    base = str(_workspace_root() or cwd)
    want = args.get("path")
    note = ""
    target = base
    if want:
        cand = want if os.path.isabs(want) else os.path.join(base, want)
        if os.path.exists(cand):
            target = cand
        else:
            note = f"path {want!r} not found — searched the whole project instead"
    limit = int(args.get("limit", 80))
    glob = args.get("glob")
    rg = _sh.which("rg")
    if rg:
        cmd = [rg, "-n", "-i", "--no-heading", "-m", str(limit)]
        for d in _SKIP_DIRS:
            cmd += ["-g", f"!{d}"]
        if glob:
            cmd += ["-g", glob]
        cmd += [pattern, target]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            lines = (p.stdout or "").splitlines()[:limit]
            return {"ok": True, "matches": lines, "note": note,
                    "truncated": len(lines) >= limit}
        except Exception:  # noqa: BLE001
            pass  # fall through to python
    # Python fallback
    try:
        rx = _re2.compile(pattern, _re2.IGNORECASE)
    except _re2.error as e:
        return {"ok": False, "error": f"bad regex: {e}"}
    out: list[str] = []
    import fnmatch as _fn
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            # fnmatch handles *.py, test_*, *_spec.ts etc. (the old
            # endswith(glob.lstrip("*")) only matched suffix globs).
            if glob and not _fn.fnmatch(f, glob):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    for i, ln in enumerate(fh, 1):
                        if rx.search(ln):
                            out.append(f"{os.path.relpath(fp, base)}:{i}:{ln.rstrip()[:200]}")
                            if len(out) >= limit:
                                return {"ok": True, "matches": out, "note": note,
                                        "truncated": True}
            except Exception:  # noqa: BLE001
                continue
    return {"ok": True, "matches": out, "note": note, "truncated": False}


def _t_remember_rule(args: dict, cwd: str) -> dict:
    """Persist a user rule that must apply to EVERY future session.
    scope: 'global' (all repos) or 'repo' (this repo only)."""
    try:
        from aiforge_core.memory import md_store
        text = (args.get("text") or args.get("rule") or "").strip()
        if not text:
            return {"ok": False, "error": "missing 'text'"}
        scope = (args.get("scope") or "global").lower()
        repo = _repo_name(cwd)
        if scope == "repo":
            source, title = f"rules:{repo}", f"{repo} — rules"
        else:
            source, title = "rules:global", "AIForge rules (all sessions)"
        md_store.append_bullet(source=source, title=title, bullet=text,
                               kind="rule", tags=["rule", scope])
        return {"ok": True, "scope": scope, "remembered": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _rules_context(cwd: str) -> str:
    """The user's persistent rule book (global + this-repo), injected into
    EVERY session so the rules are always honoured."""
    try:
        from aiforge_core.memory import md_store
        blocks = []
        for src in ("rules:global", f"rules:{_repo_name(cwd)}"):
            p = md_store._find_by_source(src)
            if p is not None:
                body = md_store._parse(p).get("body", "")
                if body.strip():
                    blocks.append(body.strip())
        if not blocks:
            return ""
        return ("RULES — the user told you to ALWAYS follow these, every "
                "session (HIGHEST priority, override defaults):\n"
                + "\n".join(blocks)[:1800])
    except Exception:  # noqa: BLE001
        return ""


def _t_ensure_runtime(args: dict, cwd: str) -> dict:
    """Install + verify missing language runtimes / build tools so the
    agent can actually build & run the project."""
    try:
        from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
        tools = args.get("tools") or args.get("tool") or []
        return ensure_runtime(tools)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_project(args: dict, cwd: str) -> dict:
    """Detect/install/build/test/run any common stack (maven, gradle,
    node/react/next/vite, python, go, rust) with the canonical command."""
    try:
        from aiforge_core.runtime.tools.project_runner import project
        return project(action=args.get("action", "detect"),
                       cwd=args.get("cwd") or cwd,
                       timeout=int(args.get("timeout", 1800)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_confluence_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_search(args, cwd)


def _t_confluence_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_read(args, cwd)


def _t_confluence_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_create(args, cwd)


def _t_confluence_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_update(args, cwd)


def _t_jira_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_search(args, cwd)


def _t_jira_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_read(args, cwd)


def _t_jira_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_create(args, cwd)


def _t_jira_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_update(args, cwd)


def _t_jira_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_comment(args, cwd)


def _t_gitlab_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_search(args, cwd)


def _t_gitlab_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_read(args, cwd)


def _t_gitlab_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_create(args, cwd)


def _t_gitlab_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_update(args, cwd)


def _t_gitlab_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_comment(args, cwd)


def _t_web_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_search(args, cwd)


def _t_web_fetch(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_fetch(args, cwd)


def _t_serve(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.serve(args, cwd)


def _t_stop_service(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.stop_service(args, cwd)


def _t_list_services(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.list_services(args, cwd)


def _t_skill_search(args: dict, cwd: str) -> dict:
    """Search the skill registry (SKILL.md playbooks) by relevance."""
    try:
        from aiforge_core.runtime import skills as _skills
        q = args.get("query") or args.get("q") or ""
        hits = _skills.search(q, cwd, k=int(args.get("k", 5)))
        return {"ok": True, "skills": hits}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_skill(args: dict, cwd: str) -> dict:
    """Author a reusable skill (SKILL.md) so future sessions reuse the
    solution. scope: 'global' (all repos) or 'repo' (this repo)."""
    try:
        from aiforge_core.runtime import skills as _skills
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        return _skills.write_skill(
            name=args.get("name", ""),
            description=args.get("description", ""),
            body=args.get("body") or args.get("content") or "",
            triggers=list(triggers),
            cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_workflow_search(args: dict, cwd: str) -> dict:
    """Search the workflow registry (WORKFLOW.md procedures) by relevance."""
    try:
        from aiforge_core.runtime import workflows as _wf
        q = args.get("query") or args.get("q") or ""
        return {"ok": True, "workflows": _wf.search(q, cwd, k=int(args.get("k", 5)))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_workflow(args: dict, cwd: str) -> dict:
    """Author a reusable workflow (WORKFLOW.md) — an end-to-end procedure —
    so future sessions (or the user) can reuse it. scope: 'global' or 'repo'."""
    try:
        from aiforge_core.runtime import workflows as _wf
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        return _wf.write_workflow(
            name=args.get("name", ""),
            description=args.get("description", ""),
            body=args.get("body") or args.get("content") or "",
            triggers=list(triggers),
            cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


TOOLS: dict[str, Callable[[dict, str], dict]] = {
    "file_read": _t_file_read,
    "file_write": _t_file_write,
    "file_create": _t_file_write,   # alias
    "file_patch": _t_file_patch,
    "list_dir": _t_list_dir,
    "find": _t_find,
    "grep": _t_grep,
    "run_command": _t_run_command,
    "ensure_runtime": _t_ensure_runtime,
    "project": _t_project,
    "remember_rule": _t_remember_rule,
    "memory_lookup": _t_memory_lookup,
    "memory_write": _t_memory_write,
    "skill_search": _t_skill_search,
    "learn_skill": _t_learn_skill,
    "confluence_search": _t_confluence_search,
    "confluence_read": _t_confluence_read,
    "confluence_create": _t_confluence_create,
    "confluence_update": _t_confluence_update,
    "jira_search": _t_jira_search,
    "jira_read": _t_jira_read,
    "jira_create": _t_jira_create,
    "jira_update": _t_jira_update,
    "jira_comment": _t_jira_comment,
    "gitlab_search": _t_gitlab_search,
    "gitlab_read": _t_gitlab_read,
    "gitlab_create": _t_gitlab_create,
    "gitlab_update": _t_gitlab_update,
    "gitlab_comment": _t_gitlab_comment,
    "web_search": _t_web_search,
    "web_fetch": _t_web_fetch,
    "workflow_search": _t_workflow_search,
    "learn_workflow": _t_learn_workflow,
    "serve": _t_serve,
    "stop_service": _t_stop_service,
    "list_services": _t_list_services,
}

_SEARCH_TOOLS = ("grep", "find", "repo_map", "graphify_lookup", "memory_lookup")
_FILE_TOOLS = ("file_read", "file_write", "file_create", "file_patch",
               "list_dir", "editor")


def _perf_family(name: str) -> str:
    """Map a tool name to a Perf-page family label (Search / File / Tool)."""
    if name in _SEARCH_TOOLS or "search" in name:
        return "Search"
    if name in _FILE_TOOLS:
        return "File"
    return "Tool"


# PLAN mode (#2): read-only tool subset — inspect + recall, never mutate.
_READONLY_TOOLS = ("file_read", "list_dir", "find", "grep", "memory_lookup",
                   "skill_search", "confluence_search", "confluence_read",
                   "jira_search", "jira_read",
                   "gitlab_search", "gitlab_read",
                   "web_search", "web_fetch", "workflow_search")

# File-mutating tools that the pre-apply "Review edits" gate (Gap D) holds for
# human Approve/Reject even when policy would auto-allow them.
_MUTATING = ("file_write", "file_create", "file_patch", "editor")

_PLAN_BANNER = (
    "PLAN MODE — you are READ-ONLY this turn. You may inspect the repo "
    "(file_read, list_dir, find, grep) and recall memory (memory_lookup), but "
    "you CANNOT write files, run commands, install, or change anything. "
    "Investigate, then produce a concrete step-by-step PLAN in FINAL: (files "
    "to touch, commands to run, tests, risks). The user switches to Act mode "
    "to execute it. ASK if you need input to plan well."
)


def _fence(body: str, lang: str = "") -> str:
    """Wrap text in a fenced code block so the markdown renderer shows it as a
    monospace block (diffs, commands, JSON) instead of reflowed prose."""
    return f"```{lang}\n{body}\n```"


def _xhtml_to_md(xhtml: str) -> str:
    """Light Confluence storage-XHTML → readable markdown, so the approval
    preview shows formatted text instead of raw ``<p>…</ac:…>`` tags."""
    import html
    import re
    s = xhtml or ""
    for i in range(6, 0, -1):                       # headings
        s = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>",
                   lambda m, i=i: "\n" + "#" * i + " " + m.group(1).strip() + "\n",
                   s, flags=re.I | re.S)
    s = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", s, flags=re.I | re.S)
    s = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", s, flags=re.I | re.S)
    s = re.sub(r'''<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>''', r"[\2](\1)",
               s, flags=re.I | re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|ul|ol|h[1-6]|tr|table|ac:[\w-]+)>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)                    # strip remaining tags + macros
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _change_diff(old: str, new: str, label: str) -> str:
    """Unified diff of ``old`` → ``new`` as a fenced ```diff block (renders as
    a colored monospace block). ``_(no change)_`` when identical."""
    import difflib
    # Bound the inputs: difflib is ~O(n·m), so a 200KB↔200KB rewrite could
    # freeze the approval gate for tens of seconds. Cap to ~20k chars each —
    # the preview is a human glance, not a full audit (the full body is still
    # what gets written/sent).
    old, new = (old or "")[:20_000], (new or "")[:20_000]
    d = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"current {label}", tofile=f"new {label}", lineterm=""))
    return _fence(d[:4000], "diff") if d.strip() else "_(no change)_"


def _fetch_current(fn, args: dict, cwd: str, timeout: float = 4.0) -> dict:
    """Best-effort fetch of an item's CURRENT state for the approval diff,
    HARD-bounded so a slow/down integration API can't stall the approval gate
    (the tool's own 20s read timeout is too long to block the operator). Runs
    the read in a worker thread and abandons it after ``timeout`` seconds —
    the preview then just shows the new content with no diff."""
    import concurrent.futures
    # NOTE: a `with ThreadPoolExecutor()` block would call shutdown(wait=True)
    # on exit and re-block until the (possibly hung) read finished — defeating
    # the timeout. Shut down WITHOUT waiting so we return immediately; the
    # worker thread finishes on its own (bounded by the tool's own 20s read).
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        r = ex.submit(fn, args, cwd).result(timeout=timeout)
        return r if isinstance(r, dict) and r.get("ok") else {}
    except Exception:  # noqa: BLE001 — timeout / read error → no diff, not a stall
        return {}
    finally:
        ex.shutdown(wait=False)


def _diff_preview(tool: str, args: dict, cwd: str) -> str:
    """Markdown preview of a mutating action for the approval gate.

    Returns markdown (the chat UI renders it): diffs/commands/JSON go in fenced
    code blocks; the integration write tools (Confluence/Jira/GitLab) get a
    readable heading + fields + body so the operator reviews formatted content,
    not a raw ``{"...": "..."}`` string dump."""
    import difflib
    try:
        if tool in ("file_write", "file_create"):
            path = args.get("path", "?")
            new = args.get("content", "")
            try:
                old = _resolve(cwd, path).read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                old = ""
            diff = "".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}"))
            if diff:
                return f"**Write `{path}`**\n\n" + _fence(diff[:4000], "diff")
            return f"**New file `{path}`** ({len(new)} bytes)\n\n" + _fence(
                str(new)[:2000])
        if tool == "file_patch":
            return (f"**Patch `{args.get('path', '?')}`**\n\n" + _fence(
                f"- {str(args.get('old_text', ''))[:1000]}\n"
                f"+ {str(args.get('new_text', ''))[:1000]}", "diff"))
        if tool in ("run_command", "bash", "shell"):
            return "**Run command**\n\n" + _fence(str(args.get("cmd", "")), "bash")

        # ── integration writes → formatted markdown, not a JSON blob ──────
        if tool == "confluence_create":
            return (f"### Create Confluence page\n\n"
                    f"**Space:** `{args.get('space', '?')}` · "
                    f"**Title:** {args.get('title', '?')}\n\n"
                    f"**Body:**\n\n"
                    + _xhtml_to_md(str(args.get('body', '')))[:3000])
        if tool == "confluence_update":
            pid = args.get("id", "?")
            new_md = _xhtml_to_md(str(args.get("body", "")))
            from aiforge_core.runtime.tools import confluence
            cur = _fetch_current(confluence.confluence_read, {"id": pid}, cwd)
            cur_md = _xhtml_to_md(str(cur.get("body") or "")) if cur else ""
            out = f"### Update Confluence page `{pid}`\n\n"
            if args.get("title"):
                out += f"**New title:** {args['title']}\n\n"
            if args.get("body") is not None:
                out += ("**Body changes:**\n\n" + _change_diff(cur_md, new_md, "body")
                        if cur_md else "**New body:**\n\n" + new_md[:3000])
            return out
        if tool == "jira_create":
            md = (f"### Create Jira issue\n\n"
                  f"**Project:** `{args.get('project', '?')}` · "
                  f"**Type:** {args.get('issuetype', 'Task')}"
                  + (f" · **Priority:** {args['priority']}" if args.get('priority') else "")
                  + f"\n\n**Summary:** {args.get('summary', '?')}\n")
            if args.get("description"):
                md += f"\n{str(args['description'])[:3000]}\n"
            if args.get("labels"):
                md += f"\n**Labels:** {args['labels']}\n"
            return md
        if tool == "jira_update":
            key = args.get("key", "?")
            from aiforge_core.runtime.tools import jira
            cur = _fetch_current(jira.jira_read, {"key": key}, cwd)
            md = f"### Update Jira issue `{key}`\n\n"
            if args.get("summary"):
                md += (f"**Summary:** {cur.get('summary', '(current)')} "
                       f"→ **{args['summary']}**\n\n")
            for k in ("priority", "assignee", "labels"):
                if args.get(k):
                    md += f"**{k.capitalize()}:** {args[k]}\n\n"
            if args.get("description") is not None:
                md += ("**Description changes:**\n\n"
                       + _change_diff(str(cur.get("description") or ""),
                                      str(args["description"]), "description"))
            return md
        if tool == "jira_comment":
            return (f"### Comment on Jira `{args.get('key', '?')}`\n\n"
                    f"{str(args.get('body', ''))[:3000]}")
        if tool == "gitlab_create":
            md = (f"### Create GitLab issue\n\n"
                  f"**Project:** `{args.get('project', '?')}`\n\n"
                  f"**Title:** {args.get('title', '?')}\n")
            if args.get("description"):
                md += f"\n{str(args['description'])[:3000]}\n"
            if args.get("labels"):
                md += f"\n**Labels:** {args['labels']}\n"
            return md
        if tool == "gitlab_update":
            proj, iid = args.get("project", "?"), args.get("iid", "?")
            from aiforge_core.runtime.tools import gitlab
            cur = _fetch_current(gitlab.gitlab_read, {"project": proj, "iid": iid}, cwd)
            md = f"### Update GitLab issue `{proj}#{iid}`\n\n"
            if args.get("title"):
                md += (f"**Title:** {cur.get('title', '(current)')} "
                       f"→ **{args['title']}**\n\n")
            for k in ("labels", "state_event"):
                if args.get(k):
                    md += f"**{k.replace('_', ' ').capitalize()}:** {args[k]}\n\n"
            if args.get("description") is not None:
                md += ("**Description changes:**\n\n"
                       + _change_diff(str(cur.get("description") or ""),
                                      str(args["description"]), "description"))
            return md
        if tool == "gitlab_comment":
            return (f"### Comment on GitLab "
                    f"`{args.get('project', '?')}#{args.get('iid', '?')}`\n\n"
                    f"{str(args.get('body', ''))[:3000]}")
    except Exception:  # noqa: BLE001
        pass
    return _fence(json.dumps(args, default=str, indent=2)[:2000], "json")

_SYSTEM = """You are AIForge, an autonomous coding assistant with FULL access to \
the user's filesystem and shell in the working directory {cwd}.

You work by emitting ONE step at a time in this exact text format.

To use a tool:
THOUGHT: <your reasoning>
ACTION: <any one tool name listed under "Tool arguments" below — files, shell,
         memory, skills/workflows, Confluence/Jira/GitLab, and web search are
         all available>
ARGS_JSON: <a single-line JSON object of the tool's arguments>

Tool arguments:
- file_read    {{"path": "rel/or/abs"}}
- file_write   {{"path": "...", "content": "..."}}      (creates/overwrites)
- file_patch   {{"path": "...", "old_text": "...", "new_text": "..."}}
- list_dir     {{"path": "."}}
- find         {{"name": "controller", "kind": "dir"}}  (fuzzy-locate files/dirs by partial name)
- grep         {{"pattern": "TODO", "path": "src"}}      (recursive; tolerates a wrong path)
- run_command  {{"cmd": "ls -la", "timeout": 600}}
- ensure_runtime {{"tools": ["java", "mvn"]}}    (install+verify missing tools)
- project        {{"action": "build"}}    (detect+install+build/test/run:
                  maven, gradle, node/react/next/vite, python, go, rust)
- remember_rule {{"text": "always use yarn", "scope": "repo"}}
                 (persist a user rule for every session; scope global|repo)
- memory_lookup{{"query": "..."}}                        (recall from knowledge memory)
- memory_write {{"text": "the durable fact", "kind": "note|gotcha|decision", "decision": false}}
                (save a learning/decision to the knowledge graph for future recall)
- skill_search {{"query": "..."}}                        (find reusable SKILL.md playbooks)
- learn_skill  {{"name": "...", "description": "when to use it", "body": "the step-by-step playbook", "triggers": ["word1","word2"], "scope": "global|repo"}}
                (author a reusable skill after solving something non-trivial — also recorded in memory)
- workflow_search {{"query": "..."}}                     (find reusable WORKFLOW.md end-to-end procedures)
- learn_workflow  {{"name": "...", "description": "when to use it", "body": "the end-to-end steps", "triggers": ["word1"], "scope": "global|repo"}}
                (author a reusable multi-step workflow when the user asks or after running a repeatable procedure)
- confluence_search {{"query": "..."}}  or  {{"cql": "space = ENG AND text ~ 'foo'"}}   (find pages)
- confluence_read   {{"id": "12345"}}  or  {{"title": "Page Title", "space": "ENG"}}      (read a page; body is storage XHTML)
- confluence_create {{"title": "...", "space": "ENG", "body": "<p>storage XHTML</p>", "parent_id": "123"}}   (new page — needs your Approve)
- confluence_update {{"id": "12345", "body": "<p>new storage XHTML</p>", "title": "optional"}}              (edit a page — needs your Approve)
- jira_search   {{"query": "..."}}  or  {{"jql": "project = ENG AND status = Open"}}   (find issues)
- jira_read     {{"key": "ENG-123"}}                                                    (read an issue: fields + comments)
- jira_create   {{"project": "ENG", "summary": "...", "issuetype": "Task", "description": "..."}}   (new issue — needs your Approve)
- jira_update   {{"key": "ENG-123", "summary": "...", "description": "...", "labels": ["a","b"]}}     (edit fields — needs your Approve)
- jira_comment  {{"key": "ENG-123", "body": "comment text"}}                            (add a comment — needs your Approve)
- gitlab_search {{"query": "..."}}  (find issues; optional "project": "group/proj", "state": "opened")
- gitlab_read   {{"project": "group/proj", "iid": 42}}                                   (read an issue: fields + comments)
- gitlab_create {{"project": "group/proj", "title": "...", "description": "...", "labels": ["a","b"]}}   (new issue — needs your Approve)
- gitlab_update {{"project": "group/proj", "iid": 42, "title": "...", "labels": ["x"], "state_event": "close"}}   (edit — needs your Approve)
- gitlab_comment{{"project": "group/proj", "iid": 42, "body": "comment text"}}            (add a comment — needs your Approve)
After any Confluence/Jira/GitLab create/update/comment SUCCEEDS, show the user a \
short AFTER preview of what was written (the `written` field in the result) plus \
the page/issue link — so they can confirm the change without opening it.
- web_search    {{"query": "rust tokio select! cancellation", "limit": 5}}   (search the open web — no key — when you're stuck / need current docs)
- web_fetch     {{"url": "https://...", "max_chars": 6000}}                  (read a result page's text)
- serve         {{"cmd": "npm run dev", "port": 5173}}   (START a server/app in the BACKGROUND; returns its pid + the URL to open — use this to run the app, NOT run_command which would block)
- stop_service  {{"pid": 12345}}                          (stop a service you started with serve)
- list_services {{}}                                      (list services you started + whether each is alive)

When stuck on an unfamiliar error, a library API, or a config flag, use \
web_search then web_fetch the most relevant hit instead of guessing.

When you are done and ready to reply to the user:
THOUGHT: <reasoning>
FINAL: <your full natural-language answer>

When the request is ambiguous, you're missing information, or you'd \
otherwise have to guess or keep retrying the same thing, ASK the user \
instead of circling:
THOUGHT: <why you need input>
ASK: <one concise, specific question>
The turn ends and the user's next message answers you.

NEVER use ASK to request permission or confirmation to proceed. Do NOT say \
things like "type yes to confirm", "shall I proceed?", "is it OK if I…", or \
"let me know if you want me to continue". Risky/mutating actions are gated \
automatically — the user gets an Approve/Reject prompt for those, so you must \
not also ask. Just emit the ACTION; the harness handles approval. ASK is ONLY \
for missing facts you genuinely cannot proceed without (which file, what \
behaviour, scope) — never for permission.

Rules: emit exactly one ACTION or one FINAL per turn. After each ACTION you \
receive an OBSERVATION with the tool result, then continue. Keep going until \
the task is complete, then give FINAL. Do real work — read and edit files, run \
commands — rather than guessing.

Operating principles — be fully autonomous, don't stop half-way:
- SESSION START: on your FIRST turn you already have, above, the repo map \
(files/folders), the project summary, and any memory recalled for this \
request — read them first so you start informed by prior sessions. If the \
request is clear, proceed. If it's ambiguous or you'd have to assume key \
details (which files/module, framework, desired behaviour, scope), ASK \
your clarifying questions UP-FRONT (ASK:) before doing work — don't guess.
- ASK, don't circle: if you're unsure what the user wants, lack a needed \
detail, or catch yourself repeating a step that isn't working, emit ASK: \
<question> and wait — never loop on the same failing action or guess at an \
ambiguous request.
- RULE BOOK: when the user says "remember…", "always…", "never…", "for \
all sessions", or states a standing rule about the folder/repo/workflow, \
immediately call remember_rule (scope=repo for this repo, scope=global for \
everywhere). Any RULES shown above are user rules — always obey them.
- PLAN then act, and RECAP: for any multi-step task, open your first THOUGHT \
with a short numbered PLAN (the steps you intend to take) so the user sees \
the approach before you change anything. End your FINAL with a one-line \
"Done:" recap of the steps you actually took. Keep both brief.
- LEARN skills + workflows (auto-improve): when you solve a non-trivial, \
repeatable problem, call learn_skill to save a reusable SKILL.md (a small \
how-to); for a full end-to-end procedure you just ran, call learn_workflow \
to save a WORKFLOW.md. Name it, give a one-line WHEN-to-use description, the \
step body, and trigger words. Before tackling unfamiliar work, skill_search \
AND workflow_search first — a saved playbook may already solve it. The \
RELEVANT SKILLS / RELEVANT WORKFLOWS shown above are auto-selected for this \
request by relevance — apply them when they fit.
- SCOPE before reading: when asked to check/review/understand code, first \
narrow to the FEW files that actually matter — use `grep`/`find` (and \
list_dir) to locate the relevant symbols/files, then read only those. Do \
NOT read every file in the repo; analysing irrelevant files wastes effort \
and context. Read broadly only when the task genuinely spans the codebase.
- When asked to RUN/BUILD/TEST a project: prefer the `project` tool — it \
auto-detects the stack (maven/gradle/node/react/next/vite/python/go/rust), \
installs the toolchain, and runs the right command. For anything it \
doesn't cover, fall back to run_command and do every step yourself \
(install deps → build → run). Execute, don't just describe.
- PROVE IT RUNS — don't make the user ask. After you write/change code (a \
POC, a feature, a bug fix), do NOT stop at "code written". Proactively: \
(1) build/compile, (2) run the tests (write them if missing — a POC still \
needs at least one test), (3) START the app with `serve` (it returns the \
pid + the URL), and (4) in your FINAL give the operator the exact \
endpoint/URL to open AND the commands to run it themselves, plus how to stop \
it (stop_service(pid)). If there are TWO services (e.g. an API + a web UI), \
`serve` BOTH and give both URLs and how they connect. Use `serve` for \
long-running servers (run_command would block); use run_command for \
one-shot build/test commands.
- WRONG/VAGUE path: if you're unsure of a folder/file name or a path \
errors, use `find` to locate it first (partial name is fine), then read or \
`grep`. `grep` already searches the whole project if the given path is \
wrong — never give up because a path was slightly off.
- MISSING RUNTIME/TOOL: if a command fails with "command not found" (java, \
mvn, python, node, go…) or you know the stack up front, call ensure_runtime \
with the executables you need (e.g. ["java","mvn"]); it installs + verifies \
them. Then re-run the build and CONTINUE the loop — finish the job.
- DELETE policy: you may do every operation autonomously EXCEPT deleting \
files or data. Never run rm / rmdir / git clean / drop table / etc. without \
the user's OK — stop and ASK in FINAL, describing the exact command; only \
proceed after they confirm.
- FIX errors yourself: if a command fails, read the error in the OBSERVATION, \
edit the offending file(s), and re-run. Loop until it actually works \
(exit 0 / server up / tests green). Install any missing tool or package on \
demand. Never hand a broken state back to the user.
- TEST what you build: after writing or changing code, verify it — call \
`project` with action "test" (or run the repo's test command). If you \
wrote new logic and there's no test for it, add a quick test and run it. \
If you CANNOT determine how to test (no test framework/files — check \
`project` detect's has_tests), ASK the user: where and how should I test \
this, or should I skip tests? Do not silently skip verification.
- When asked to PUSH (or "commit and push"): use run_command with git — \
`git add -A`, `git commit -m "<concise message>"`, then `git push`. If not on \
a branch or push is rejected, create/switch a branch and push that. Report \
the branch + result in FINAL.
- Verify before claiming done: re-run the build/test/run command and confirm \
it succeeded from the OBSERVATION, then summarize what you did in FINAL."""


def _balanced_json(text: str, start_at: int = 0) -> dict:
    """Extract + parse the first balanced {...} object at/after start_at.
    Brace-counting (string-aware) so it survives code fences, trailing
    junk, pretty-printed/multiline JSON, and braces inside strings.
    Returns {} when none parses."""
    start = text.find("{", start_at)
    if start < 0:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return {}
    return {}


def _parse(out: str) -> dict:
    """Parse a model turn into {kind, ...}. Tolerant of code fences,
    pretty-printed JSON, and stray markdown around the protocol."""
    fin = _FINAL_RE.search(out)
    ask = _ASK_RE.search(out)
    act = _ACTION_RE.search(out)
    # Prefer ACTION when present (models sometimes mention "final" in prose).
    if act:
        name = act.group(1).strip()
        # Args = first balanced {...} after the ARGS_JSON marker if present,
        # else after the ACTION line. Handles ```json fenced args.
        m = re.search(r"ARGS_JSON\s*:?", out, re.IGNORECASE)
        args = _balanced_json(out, m.end() if m else act.end())
        thought = _THOUGHT_RE.search(out)
        return {"kind": "action", "tool": name, "args": args,
                "thought": thought.group(1).strip() if thought else ""}
    if ask:
        return {"kind": "ask", "text": ask.group(1).strip()}
    if fin:
        return {"kind": "final", "text": fin.group(1).strip()}
    # No protocol markers — treat the whole output as the final answer.
    return {"kind": "final", "text": out.strip()}


# Loop detection: no fixed step budget — long coding sessions run until
# the agent finishes. We stop only when it's clearly STUCK: the same
# tool+args repeated this many times, or identical model output N times
# in a row. ``_SAFETY_CAP`` is a last-resort runaway guard (very high;
# tune with AIFORGE_CHAT_SAFETY_CAP), not a normal stopping point.
_LOOP_REPEAT = 4
_OUTPUT_REPEAT = 3


_CONDENSE_OPEN = "<<AIFORGE_CTX_CONDENSED>>"
_CONDENSE_CLOSE = "<</AIFORGE_CTX_CONDENSED>>"


def _ctx_budget_chars() -> int:
    """Char budget for the running conversation before auto-condensing.
    ~48k chars ≈ ~12k tokens — safe headroom under most context windows.
    0 disables. Tunable via AIFORGE_CHAT_CONTEXT_BUDGET_CHARS."""
    try:
        return int(os.environ.get("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "48000"))
    except ValueError:
        return 48000


def _compact_convo(convo: list[dict], *, keep_recent: int = 12) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — no extra LLM call, so
    it's cheap and runs every turn. The agent can re-read files / ask the user
    if it needs detail from before the condense point."""
    budget = _ctx_budget_chars()
    if budget <= 0 or len(convo) <= keep_recent + 2:
        return convo
    if sum(len(m.get("content") or "") for m in convo) <= budget:
        return convo
    tail = convo[-keep_recent:]
    middle = convo[1:-keep_recent]
    if not middle:
        return convo
    tools: list[str] = []
    for m in middle:
        if m.get("role") == "assistant":
            mt = _ACTION_RE.search(m.get("content") or "")
            if mt:
                tools.append(mt.group(1))
    import collections as _c
    used = ", ".join(f"{t}×{n}" for t, n in _c.Counter(tools).most_common(8)) \
        or "discussion + reads"
    # Wrap the breadcrumb in a unique sentinel so the next condense can strip
    # exactly THIS block (not a look-alike phrase a rule/skill might contain).
    note = (f"{_CONDENSE_OPEN}\n"
            "[earlier conversation auto-condensed to fit the context window — "
            f"{len(middle)} messages omitted. Work done so far: {used}. "
            "Re-read a file or ask the user if you need detail from before "
            f"this point.]\n{_CONDENSE_CLOSE}")
    # Fold the breadcrumb INTO the system message rather than inserting a
    # separate 'user' turn — that avoids two consecutive same-role messages
    # (some providers reject those) and keeps the tail's alternation intact.
    # Strip any prior sentinel block first so the system message can't grow
    # unbounded across repeated condenses.
    sys_text = re.sub(
        re.escape(_CONDENSE_OPEN) + r".*?" + re.escape(_CONDENSE_CLOSE),
        "", convo[0].get("content") or "", flags=re.S).rstrip()
    head = [{"role": "system", "content": (sys_text + "\n\n" + note).strip()}]
    return head + tail


def _build_repo_map(cwd: str, max_entries: int = 240, max_depth: int = 3) -> str:
    """A compact directory tree of ``cwd`` for the system prompt, so the
    agent has the repo structure in context every turn (no re-searching).
    Skips junk dirs, caps entries + depth. Best-effort."""
    base = str(_workspace_root() or cwd)
    if not os.path.isdir(base):
        return f"WORKING DIRECTORY: {base} (not a directory)"
    lines: list[str] = []
    base_depth = base.rstrip(os.sep).count(os.sep)
    try:
        for root, dirs, files in os.walk(base):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                             and not d.startswith("."))
            rel = os.path.relpath(root, base)
            indent = "" if rel == "." else "  " * depth
            if rel != ".":
                lines.append(f"{indent}{os.path.basename(root)}/")
            for f in sorted(files)[:40]:
                if not f.startswith("."):
                    lines.append(f"{indent}  {f}")
            if len(lines) >= max_entries:
                lines.append("  … (truncated — use find/grep/list_dir for more)")
                break
    except Exception:  # noqa: BLE001
        pass
    tree = "\n".join(lines) or "(empty)"
    return ("REPO MAP of the working directory (already known — do NOT "
            f"re-list directories you can see here):\nWORKING DIRECTORY: {base}\n"
            f"{tree}")


def _repo_name(cwd: str) -> str:
    base = str(_workspace_root() or cwd).rstrip(os.sep)
    return os.path.basename(base) or "repo"


def _memory_recall(cwd: str, query: str, limit: int = 6) -> str:
    """Proactive memory recall at SESSION START — pull prior decisions /
    gotchas / learnings relevant to the user's opening request so the agent
    arrives informed (self-learning) instead of re-deriving what past
    sessions already worked out. Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    hits: list[dict] = []
    try:
        from aiforge_core.memory import unified_query as _uq
        res = _uq.query(q, limit=limit)
        if isinstance(res, dict):
            hits = res.get("hits", []) or []
    except Exception:  # noqa: BLE001
        hits = []
    lines: list[str] = []
    for h in hits:
        txt = (h.get("text") or "").strip().replace("\n", " ")
        if not txt:
            continue
        src = h.get("source") or ""
        lines.append(f"- {txt[:240]}" + (f"  ({src})" if src else ""))
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return ("RELEVANT MEMORY recalled for this request (prior decisions / "
            "gotchas / learnings from earlier sessions — consult before "
            "re-deriving):\n" + "\n".join(lines))


def _repo_context(cwd: str) -> str:
    """The persistent PROJECT SUMMARY for this repo — what it is + what's
    been done — injected every turn so follow-ups have continuity. Read
    from the per-repo memory file (source=repo:<name>); if none exists yet,
    auto-build a starter from the detected stack + README so there's always
    something. The summary is updated at the end of each session run."""
    base = str(_workspace_root() or cwd)
    repo = _repo_name(cwd)
    try:
        from aiforge_core.memory import md_store
        p = md_store._find_by_source(f"repo:{repo}")
        if p is not None:
            body = md_store._parse(p).get("body", "")
            if body.strip():
                return (f"PROJECT SUMMARY — {repo} (what this repo is + what "
                        f"prior sessions did):\n{body[:2500]}")
    except Exception:  # noqa: BLE001
        pass
    # Starter (first time): stack + README excerpt.
    stacks: list[str] = []
    try:
        from aiforge_core.runtime.tools.project_runner import detect
        stacks = detect(base).get("stacks", [])
    except Exception:  # noqa: BLE001
        pass
    readme = ""
    for rn in ("README.md", "Readme.md", "readme.md", "README.rst", "README.txt"):
        rp = os.path.join(base, rn)
        if os.path.isfile(rp):
            try:
                readme = open(rp, encoding="utf-8", errors="ignore").read()[:700]
            except Exception:  # noqa: BLE001
                pass
            break
    out = f"PROJECT SUMMARY — {repo} (auto-detected; refine as you learn):\n"
    out += f"- Stack(s): {', '.join(stacks) or 'unknown'}\n"
    if readme:
        out += f"- README excerpt:\n{readme}\n"
    return out


def run_chat_agent(
    messages: list[dict], *,
    cwd: str,
    role: str = "doer",
    max_steps: int | None = None,   # kept for callers/tests; None = no cap
    complete_fn: Callable[..., str] | None = None,
    session_id: int | None = None,
    mode: str = "act",              # "act" = full tools; "plan" = read-only
) -> Iterator[dict]:
    """Drive the ReAct loop until the agent finishes or a stuck loop is
    detected (NOT a step count). Yields SSE-ready event dicts:

    ``{"type": "thought", "text"}`` · ``{"type": "tool", "name", "args",
    "result"}`` · ``{"type": "message", "text"}`` (final) ·
    ``{"type": "approval", ...}`` (ask-policy gate) ·
    ``{"type": "error", "text"}`` · ``{"type": "done"}``.
    """
    if complete_fn is None:
        from aiforge_core.llm.client import complete as complete_fn  # type: ignore

    from aiforge_core.runtime import chat_approve, chat_cancel, chat_interject
    from aiforge_core.runtime.tools import tool_policy
    chat_cancel.set_active(session_id)
    plan_mode = (mode or "act").lower() == "plan"

    import collections
    safety = max_steps or int(os.environ.get("AIFORGE_CHAT_SAFETY_CAP", "2000"))

    # Latest user message drives mentions (#4) + microagent triggers (#6).
    last_user = next(
        (m.get("content") for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    rules = _rules_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    if plan_mode:                   # plan banner second — constrains this turn
        sys_msg = _PLAN_BANNER + "\n\n" + sys_msg
    sys_msg += "\n\n" + _repo_context(cwd) + "\n\n" + _build_repo_map(cwd)
    # Skills registry: always-on skills + the ones most relevant to this
    # request (SKILL.md standard, relevance-searched; folds in legacy
    # microagents). The agent can also skill_search / learn_skill at runtime.
    try:
        from aiforge_core.runtime import skills as _skills
        sk_block = _skills.auto_context(last_user, cwd)
        if sk_block:
            sys_msg += "\n\n" + sk_block
    except Exception:  # noqa: BLE001
        pass
    # Workflows registry: same treatment as skills — surface the relevant
    # reusable end-to-end procedures so the agent applies them (it can also
    # workflow_search / learn_workflow at runtime).
    try:
        from aiforge_core.runtime import workflows as _workflows
        wf_block = _workflows.auto_context(last_user, cwd)
        if wf_block:
            sys_msg += "\n\n" + wf_block
    except Exception:  # noqa: BLE001
        pass
    # @-mentions (#4): user-referenced files/folders/urls/problems.
    try:
        from aiforge_core.runtime import mentions as _mentions
        ment_block, _toks = _mentions.expand(last_user, cwd)
        if ment_block:
            sys_msg += "\n\n" + ment_block
    except Exception:  # noqa: BLE001
        pass
    # SESSION START (self-learning): on a fresh session — before any
    # assistant turn — proactively recall memory keyed to the opening
    # request, so the agent starts informed by prior sessions (it already
    # has the repo map + project summary above for files/folders). Once the
    # conversation has assistant turns the recall has already happened.
    is_init = not any((m.get("role") == "assistant") for m in messages)
    if is_init:
        first_user = next(
            (m.get("content") for m in messages
             if (m.get("role") or "user") == "user" and m.get("content")),
            "")
        recall = _memory_recall(cwd, first_user)
        if recall:
            sys_msg += "\n\n" + recall
    convo: list[dict] = [{"role": "system", "content": sys_msg}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})

    action_counts: dict[str, int] = {}
    recent_outputs: collections.deque = collections.deque(maxlen=_OUTPUT_REPEAT)
    condensed_notified = False

    n = 0
    while n < safety:
        n += 1
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            yield {"type": "error", "text": "stopped by user"}
            yield {"type": "done"}
            return
        # Mid-run steering (Gap A): fold any user-injected guidance into the
        # working context as a user turn BEFORE the next model call, so the
        # agent adjusts course without a Stop + new turn. Surface it so the UI
        # shows the steer was applied.
        if session_id is not None:
            for steer in chat_interject.drain(session_id):
                # If the last turn is already a user message (e.g. the
                # OBSERVATION we just appended after a tool step), MERGE the
                # steer into it — two consecutive user turns break some
                # providers (claude_local). Otherwise append a fresh user turn.
                if convo and convo[-1].get("role") == "user":
                    convo[-1]["content"] += f"\n\n[steer] {steer}"
                else:
                    convo.append({"role": "user", "content": f"[steer] {steer}"})
                yield {"type": "thought", "role": "steer", "text": steer}
        # Auto-condense the running history before the call so a long session
        # can't overflow the model's context window (MUST). Tell the user it
        # happened (one-time per condense) for transparency.
        _before = len(convo)
        convo = _compact_convo(convo)
        if len(convo) < _before and not condensed_notified:
            condensed_notified = True   # notify ONCE, not every over-budget turn
            yield {"type": "thought", "role": "system",
                   "text": "⚙ condensed earlier context to stay within the window"}
        try:
            out = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "text": f"llm error: {exc}"}
            yield {"type": "done"}
            return

        # Stuck-output loop: identical model reply N times running. Rather
        # than just bailing, ASK the user for guidance (don't circle).
        recent_outputs.append(out.strip())
        if (len(recent_outputs) == _OUTPUT_REPEAT
                and len(set(recent_outputs)) == 1):
            yield {"type": "message", "awaiting_input": True,
                   "text": "I seem to be going in circles on this. Could you "
                           "clarify what you'd like me to do, or give a bit "
                           "more detail? (I stopped rather than keep retrying "
                           "the same thing.)"}
            yield {"type": "done"}
            return

        convo.append({"role": "assistant", "content": out})
        step = _parse(out)
        if step["kind"] == "final":
            yield {"type": "message", "text": step["text"]}
            yield {"type": "done"}
            return
        if step["kind"] == "ask":
            # Agent is asking the user a question — show it + wait for the
            # next message (which answers it). awaiting_input flags the UI.
            yield {"type": "message", "awaiting_input": True,
                   "text": step["text"]}
            yield {"type": "done"}
            return

        # action
        name = step["tool"]
        args = step["args"]
        # Stuck-action loop: same tool+args repeated too many times → ask
        # the user instead of looping to the safety cap.
        sig = name + "|" + json.dumps(args, sort_keys=True, default=str)
        action_counts[sig] = action_counts.get(sig, 0) + 1
        if action_counts[sig] >= _LOOP_REPEAT:
            yield {"type": "message", "awaiting_input": True,
                   "text": f"I keep trying the same step (`{name}`) without "
                           "progress. I've paused — could you clarify or tell "
                           "me how you'd like me to proceed?"}
            yield {"type": "done"}
            return

        if step.get("thought"):
            yield {"type": "thought", "text": step["thought"]}

        # PLAN mode (#2): block mutating tools — read-only only.
        if plan_mode and name not in _READONLY_TOOLS:
            result = {"ok": False, "blocked": "plan_mode",
                      "error": f"'{name}' is blocked in Plan mode (read-only). "
                               "Finish with a PLAN; the user will switch to Act "
                               "mode to execute it."}
            yield {"type": "tool", "name": name, "args": args, "result": result}
            convo.append({"role": "user",
                          "content": f"OBSERVATION: {json.dumps(result)}"})
            continue

        # Permission policy (#5) + risk (#7): allow / ask / deny.
        verdict = tool_policy.decide(name, args)
        if verdict["policy"] == tool_policy.DENY:
            result = {"ok": False, "blocked": "policy",
                      "error": f"'{name}' is denied by policy: {verdict['reason']}"}
            yield {"type": "tool", "name": name, "args": args, "result": result}
            convo.append({"role": "user",
                          "content": f"OBSERVATION: {json.dumps(result)}"})
            continue
        # Pre-apply review mode (Gap D): when armed for this session, force the
        # approval gate for any mutating tool even if policy would auto-allow.
        _force_review = (session_id is not None and name in _MUTATING
                         and chat_approve.review_edits(session_id))
        # Destructive delete (rm -rf, etc): the run_command tool has its OWN
        # confirm_delete arg gate (delete_guard). If we don't route it through
        # the approval gate AND mark it confirmed on approve, the tool keeps
        # refusing ("re-issue with confirm_delete=true") and the model loops
        # asking the user to "type yes" forever. So always gate it, and let the
        # human's Approve BE the confirmation.
        _destructive_del = False
        if name in ("run_command", "bash", "run_shell", "shell"):
            try:
                from aiforge_core.runtime.tools import delete_guard
                _cmd = args.get("cmd") or args.get("command") or ""
                _destructive_del = (not delete_guard.allow_delete(
                    ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
                    and delete_guard.is_destructive_delete(_cmd))
            except Exception:  # noqa: BLE001
                _destructive_del = False
        if verdict["policy"] == tool_policy.ASK or _force_review or _destructive_del:
            # Approval gate (#1): surface the action + diff preview, block on
            # the user's Approve/Reject (POST /api/chat/sessions/{id}/approve).
            preview = _diff_preview(name, args, cwd)
            seq = chat_approve.request(session_id) if session_id is not None else 0
            _reason = (verdict["reason"] if verdict["policy"] == tool_policy.ASK
                       else "Confirm this destructive delete before it runs."
                       if _destructive_del
                       else "Review edits: confirm this file change before it lands.")
            yield {"type": "approval", "id": seq, "name": name, "args": args,
                   "reason": _reason, "preview": preview}
            if session_id is None:
                decision = {"decision": "reject", "note": "no session"}
            else:
                decision = chat_approve.wait(session_id)
            if decision.get("decision") != "approve":
                result = {"ok": False, "rejected": True,
                          "error": "user rejected this action"
                                   + (f": {decision['note']}" if decision.get("note") else "")}
                yield {"type": "tool", "name": name, "args": args, "result": result}
                convo.append({"role": "user",
                              "content": f"OBSERVATION: {json.dumps(result)} "
                                         "(the user rejected it — do NOT retry; "
                                         "adjust or ASK what they want instead.)"})
                continue
            # Approved → the human's Accept IS the delete confirmation, so
            # satisfy the run_command tool's confirm_delete gate (otherwise it
            # re-refuses and the model loops asking the user again).
            if _destructive_del:
                args["confirm_delete"] = True

        fn = TOOLS.get(name)
        if fn is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            _perf_t0 = time.perf_counter()
            try:
                result = fn(args, cwd)
            except KeyError as exc:
                result = {"ok": False, "error": f"missing arg: {exc}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            try:
                from aiforge_core.runtime import perf_recorder
                perf_recorder.record(
                    _perf_family(name), name,
                    (time.perf_counter() - _perf_t0) * 1000.0)
            except Exception:  # noqa: BLE001 — perf must never break a run
                pass
        yield {"type": "tool", "name": name, "args": args, "result": result}
        obs = json.dumps(result)[:_MAX_OBS]
        convo.append({"role": "user", "content": f"OBSERVATION: {obs}"})

    yield {"type": "message",
           "text": "(stopped: hit the runaway safety cap — "
                   "raise AIFORGE_CHAT_SAFETY_CAP if this was real work)"}
    yield {"type": "done"}
