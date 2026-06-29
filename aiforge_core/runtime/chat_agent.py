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


# ─── Chat-side commit hygiene: REFUSE blanket git stages ─────────
#
# The chat agent runs in the user's (possibly dirty) repo and writes files via
# BOTH the file tools AND the shell. A blanket ``git add -A`` / ``git add .`` /
# ``git commit -a`` issued by the model would sweep the user's UNRELATED edits
# (and the agent's own artifacts) into a commit. Rather than try to REWRITE the
# command to stage only the agent's files — fragile, the source of repeated
# edge-case bugs — we simply REFUSE the blanket stage (fail-CLOSED) and tell the
# model to stage specific paths. The system prompt already instructs this, and
# the agent loop turns the refusal into an observation so the model re-issues a
# targeted ``git add <paths>`` (which runs normally).

_BLANKET_ADD_SELECTORS = frozenset({"-A", "--all", "."})
# ``git`` global options that consume a following value, so we can skip past a
# leading ``-C <dir>`` / ``-c k=v`` to reach the SUBCOMMAND.
_GIT_GLOBAL_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Split a (quote/heredoc-masked) command into simple-command chunks on every
# shell separator AND subshell/group punctuation, so a blanket stage nested in
# ``(...)`` / ``{...}`` — including the no-separator ``(git add -A)`` form — is
# still seen.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|()\{\}\n]")


def _mask_noncode(cmd: str) -> str:
    """Return ``cmd`` with quoted-string and heredoc-body regions blanked to
    spaces (length-preserving), so a ``git add -A`` living inside ``echo "…"``
    or a heredoc body is NOT scanned as a real command."""
    out = list(cmd)
    n = len(cmd)
    i = 0
    pending: list[tuple[str, bool]] = []   # (heredoc delimiter, strip-tabs)

    def _blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = cmd[i]
        if ch == "\n" and pending:
            i += 1
            for delim, strip_tabs in pending:
                while i < n:
                    eol = cmd.find("\n", i)
                    line_end = eol if eol != -1 else n
                    line = cmd[i:line_end]
                    _blank(i, line_end)            # blank the heredoc body line
                    i = line_end + 1 if eol != -1 else n
                    cmp_line = line.lstrip("\t") if strip_tabs else line
                    if cmp_line.strip() == delim:
                        break
            pending = []
            continue
        if ch == "'":
            j = cmd.find("'", i + 1)
            end = (j + 1) if j != -1 else n
            _blank(i, end)
            i = end
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if cmd[j] == "\\":
                    j += 2
                    continue
                if cmd[j] == '"':
                    break
                j += 1
            end = (j + 1) if j < n else n
            _blank(i, end)
            i = end
            continue
        if ch == "\\":
            i += 2
            continue
        if cmd[i:i + 2] == "<<":
            m = re.match(r"<<-?", cmd[i:])
            op = m.group(0)
            i += len(op)
            while i < n and cmd[i] in " \t":
                i += 1
            dm = re.match(r"""(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""", cmd[i:])
            if dm:
                pending.append((dm.group(2), op.endswith("-")))
                i += dm.end()
            continue
        i += 1
    return "".join(out)


def _is_blanket_git(cmd: str) -> bool:
    """True when ``cmd`` would run a BLANKET git stage — ``git add -A|.|--all``
    (in any form: ``git add -A .``, ``git add -- .`` …) or ``git commit`` with
    auto-stage (``-a`` / ``-am`` / ``--all``).

    Fail-CLOSED: the stage is detected ANYWHERE in the command — top level OR
    inside a ``(...)`` / ``{...}`` subshell (so the no-separator ``(git add
    -A)`` form is caught) — and through benign ``sudo`` / ``env=val`` /
    ``git -C <dir>`` prefixes. Quote/heredoc-aware, so ``echo "git add -A"`` and
    a blanket add inside a heredoc body are NOT flagged. A targeted ``git add
    <paths>`` and a plain ``git commit`` (no ``-a``) run normally."""
    for seg in _SEGMENT_SPLIT_RE.split(_mask_noncode(cmd or "")):
        toks = seg.split()
        # Peel benign leading prefixes: ``env=val`` assignments + ``sudo`` (and
        # its dash-flags) — so ``sudo git add -A`` / ``FOO=bar git add -A`` are
        # not mistaken for a non-git command.
        k = 0
        while k < len(toks):
            t = toks[k]
            if _ENV_ASSIGN_RE.match(t):
                k += 1
                continue
            if t == "sudo":
                k += 1
                while k < len(toks) and toks[k].startswith("-"):
                    k += 1
                continue
            break
        toks = toks[k:]
        if len(toks) < 2 or toks[0] != "git":
            continue
        i = 1                                  # skip leading ``-C <dir>`` globals
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in _GIT_GLOBAL_VALUE_OPTS else 1
        if i >= len(toks):
            continue
        sub = toks[i]
        after = toks[i + 1:]
        if sub == "add":
            allowed = _BLANKET_ADD_SELECTORS | {"--"}
            if after and all(a in allowed for a in after) \
                    and any(a in _BLANKET_ADD_SELECTORS for a in after):
                return True
        elif sub == "commit":
            for a in after:
                if a == "--all":
                    return True
                if a.startswith("-") and not a.startswith("--") and "a" in a:
                    return True
    return False


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
    # Commit hygiene: REFUSE a blanket git stage. A `git add -A` / `git add .` /
    # `git commit -a` in chat would sweep the user's UNRELATED files (and the
    # agent's own artifacts) into a commit. We do NOT execute it — we return a
    # soft error so the agent loop re-issues a targeted `git add <paths>` (the
    # system prompt already instructs specific staging). Fail-CLOSED: if a
    # blanket add could run, refuse. A targeted `git add <paths>` runs normally.
    if _is_blanket_git(cmd):
        return {"ok": False, "blocked": "blanket_git",
                "error": "Blanket staging (git add -A / git add . / "
                "git commit -a) is disabled in chat to avoid committing "
                "unrelated files. Stage ONLY the files you changed: "
                "`git add <path1> <path2>` then `git commit -m \"...\"` then "
                "`git push`."}
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


def _t_gitlab_mr_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_create(args, cwd)


def _t_gitlab_mr_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_comment(args, cwd)


def _t_github_pr(args: dict, cwd: str) -> dict:
    """Open a GitHub pull request from the current branch via the ``gh`` CLI.
    Args: title (req), body, base (default 'main'), head (default current
    branch), draft. Requires gh installed + authenticated in the repo."""
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    import shutil
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh_not_installed",
                "hint": "install the GitHub CLI (gh) + `gh auth login`"}
    cmd = ["gh", "pr", "create", "--title", str(args["title"]),
           "--body", str(args.get("body") or "")]
    cmd += ["--base", str(args.get("base") or "main")]
    if args.get("head"):
        cmd += ["--head", str(args["head"])]
    if args.get("draft"):
        cmd += ["--draft"]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or out or "gh failed").strip()[:800]}
    return {"ok": True, "url": out, "written": {"title": args.get("title"),
            "base": args.get("base") or "main"}}


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


# ─────────────────────────── shared "strong" tools ──────────────────────────
# The OpenHands-parity tools (editor with undo + syntax-check, LSP, typecheck,
# format, test-runner, IPython) lived only in the ADK team pipeline. These thin
# adapters expose them to the deploy-anywhere chat agent too. They clamp to
# sandbox.root(), so we point that at the session cwd for the call.

def _with_root(cwd: str):
    try:
        from aiforge_core.runtime import sandbox
        sandbox.set_root_override(cwd)
    except Exception:  # noqa: BLE001
        pass


def _t_editor(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.editor import editor
    return editor(
        command=str(args.get("command") or args.get("sub_command") or "view"),
        path=str(args.get("path") or ""),
        file_text=args.get("file_text") if args.get("file_text") is not None else args.get("content"),
        old_str=args.get("old_str") if args.get("old_str") is not None else args.get("old_text"),
        new_str=args.get("new_str") if args.get("new_str") is not None else args.get("new_text"),
        insert_line=args.get("insert_line"),
        view_range=args.get("view_range"),
    )


def _t_multi_edit(args: dict, cwd: str) -> dict:
    """Apply a BATCH of find/replace edits across one or more files in a single
    call — validated first, then applied all-or-nothing. Each edit:
    ``{"path","old_str","new_str","replace_all"?}``. Replaces the round-trip
    pain of one-edit-per-turn and the "exactly one match" failures."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list of "
                "{path, old_str, new_str, replace_all?}"}
    # Phase 1 — validate every edit against current disk content (no writes).
    plans: list[tuple[str, str]] = []   # (abs_path, new_content)
    pending: dict[str, str] = {}        # abs_path -> working content (chained)
    rel_of: dict[str, str] = {}         # abs_path -> the path the model gave
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return {"ok": False, "error": f"edit #{i} is not an object"}
        path = str(e.get("path") or "").strip()
        old = e.get("old_str") if e.get("old_str") is not None else e.get("old_text")
        new = e.get("new_str") if e.get("new_str") is not None else e.get("new_text")
        if not path or old is None or new is None:
            return {"ok": False, "error": f"edit #{i} needs path + old_str + new_str"}
        try:
            ap = str(_resolve(cwd, path))
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        rel_of.setdefault(ap, path)
        if ap not in pending:
            try:
                pending[ap] = Path(ap).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return {"ok": False, "error": f"edit #{i}: file not found: {path}"}
        body = pending[ap]
        cnt = body.count(old)
        if cnt == 0:
            return {"ok": False, "error": f"edit #{i}: old_str not found in {path}"}
        if cnt > 1 and not e.get("replace_all"):
            return {"ok": False, "error": f"edit #{i}: old_str appears {cnt}× in "
                    f"{path} — pass replace_all:true or make it unique"}
        pending[ap] = body.replace(old, new) if e.get("replace_all") else body.replace(old, new, 1)
    plans = list(pending.items())
    # Phase 2 — all validated, write them.
    written = []
    for ap, content in plans:
        Path(ap).write_text(content, encoding="utf-8")
        written.append(rel_of.get(ap, ap))
    return {"ok": True, "files": written, "edits_applied": len(edits)}


def _t_typecheck(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.typecheck import typecheck
    return typecheck()


def _t_format(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.format import format as _fmt
    return _fmt(str(args.get("path") or "."))


def _t_lsp(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.lsp import lsp
    return lsp(command=str(args.get("command") or ""), path=str(args.get("path") or ""),
               line=int(args.get("line") or 0), character=int(args.get("character") or 0))


def _t_run_tests(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.test_runner import run_tests
    return run_tests(mode=str(args.get("mode") or "fast"), pattern=str(args.get("pattern") or ""))


def _t_ipython(args: dict, cwd: str) -> dict:
    _with_root(cwd)
    from aiforge_core.runtime.tools.ipython_kernel import execute_ipython_cell
    return execute_ipython_cell(str(args.get("code") or ""),
                                _run_id=f"chat-{args.get('_session_id') or 'default'}")


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
    "gitlab_mr_create": _t_gitlab_mr_create,
    "gitlab_mr_comment": _t_gitlab_mr_comment,
    "github_pr": _t_github_pr,
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
    # Shared "strong" tools (now available to the chat agent, not just the team
    # pipeline): structured editor (undo + syntax-check), symbols, types, tests.
    "editor": _t_editor,
    "multi_edit": _t_multi_edit,
    "typecheck": _t_typecheck,
    "format": _t_format,
    "lsp": _t_lsp,
    "run_tests": _t_run_tests,
    "ipython": _t_ipython,
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
                   "web_search", "web_fetch", "workflow_search",
                   "lsp", "typecheck")   # code-intel: read-only, OK in plan mode

# File-mutating tools that the pre-apply "Review edits" gate (Gap D) holds for
# human Approve/Reject even when policy would auto-allow them.
_MUTATING = ("file_write", "file_create", "file_patch", "editor", "multi_edit")

# The ``editor`` tool multiplexes read + write sub-commands on one tool NAME;
# only the WRITE sub-commands mutate (view/read/list are read-only and must
# NOT be held by the review-edits gate).
_EDITOR_READONLY_CMDS = ("view", "read", "list", "ls", "cat", "open")


def _editor_is_write(args: dict) -> bool:
    cmd = str((args or {}).get("command")
              or (args or {}).get("sub_command") or "").strip().lower()
    return cmd not in _EDITOR_READONLY_CMDS


def _is_mutating(name: str, args: dict) -> bool:
    """True when this tool call actually writes — ``editor`` view/read is
    read-only; every other name in ``_MUTATING`` always mutates."""
    if name not in _MUTATING:
        return False
    if name == "editor":
        return _editor_is_write(args)
    return True

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
- multi_edit   {{"edits": [{{"path":"a.py","old_str":"foo","new_str":"bar"}}, {{"path":"b.py","old_str":"x","new_str":"y","replace_all":true}}]}}
                (apply several find/replace edits across one or MANY files in ONE call — validated first, then all-or-nothing)
- list_dir     {{"path": "."}}
- find         {{"name": "controller", "kind": "dir"}}  (fuzzy-locate files/dirs by partial name)
- grep         {{"pattern": "TODO", "path": "src"}}      (recursive; tolerates a wrong path)
- run_command  {{"cmd": "ls -la", "timeout": 600}}
- ensure_runtime {{"tools": ["java", "mvn"]}}    (install+verify missing tools)
- project        {{"action": "build"}}    (detect+install+build/test/run:
                  maven, gradle, node/react/next/vite, python, go, rust)
- editor         {{"command": "str_replace", "path": "...", "old_str": "...", "new_str": "..."}}
                 (PREFER over file_patch for edits: structured file editor with
                  syntax-check before write + UNDO. command: view | create
                  {{"file_text"}} | str_replace {{"old_str","new_str"}} |
                  insert {{"insert_line","new_str"}} | undo_edit)
- run_tests      {{"mode": "fast", "pattern": "test_name"}}   (run the project's tests; mode fast|all|discover, optional -k/-Dtest pattern)
- typecheck      {{}}                                        (run the project's type-checker — tsc/mypy/go vet etc.)
- format         {{"path": "src/foo.py"}}                    (auto-format a file — ruff/prettier/gofmt)
- lsp            {{"command": "goto_definition", "path": "src/x.py", "line": 0, "character": 0}}
                 (symbol navigation: goto_definition | find_references | hover; 0-indexed)
- ipython        {{"code": "import pandas as pd; df.head()"}}  (persistent Python REPL — state survives across calls)
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
- gitlab_mr_create {{"project": "group/proj", "source_branch": "feat/x", "target_branch": "main", "title": "...", "description": "..."}}   (open a merge request — needs your Approve)
- gitlab_mr_comment{{"project": "group/proj", "iid": 7, "body": "..."}}                    (comment on an MR — needs your Approve)
- github_pr     {{"title": "...", "body": "...", "base": "main", "draft": false}}          (open a GitHub PR from the current branch via gh CLI — needs your Approve)
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
stage ONLY the specific files you created or edited \
(`git add <those exact paths>`), then `git commit -m "<concise message>"`, \
then `git push`. NEVER `git add -A` or `git add .` — that sweeps in unrelated \
changes and the agent's own artifacts. If not on a branch or push is \
rejected, create/switch a branch and push that. Report the branch + result \
in FINAL.
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


_CANCELLED = object()   # sentinel: generation abandoned because Stop was pressed

# Bound on concurrent generation threads (live + abandoned-but-still-running).
# H1 abandons a cancelled LLM call to a daemon thread; the underlying urllib
# request can't be interrupted, so it keeps a connection until it returns/times
# out (AIFORGE_LLM_TIMEOUT_S). This semaphore stops spam Stop+resend from
# stacking UNBOUNDED zombie generations: a new one waits for a slot (i.e. for a
# zombie to finish) — which matches reality on a serialized local backend. The
# wait itself is cancellable.
_GEN_SEM = None


def _gen_sem():
    global _GEN_SEM
    if _GEN_SEM is None:
        try:
            _n = max(1, int(os.environ.get("AIFORGE_CHAT_MAX_INFLIGHT_GEN", "3")))
        except ValueError:
            _n = 3
        _GEN_SEM = __import__("threading").BoundedSemaphore(_n)
    return _GEN_SEM


def _complete_cancellable(complete_fn, role, convo, session_id):
    """Run the (synchronous, uncancellable) LLM call on a side thread so a Stop
    can interrupt it. H1: previously the cancel flag was only checked between
    ReAct steps, so on a slow local model Stop appeared dead for the WHOLE
    generation (minutes). Now we poll the cancel token while the call runs and
    return the ``_CANCELLED`` sentinel the instant it's set — abandoning the
    call (it finishes in the background, daemon thread, result ignored). The
    sentinel (not ``None``) keeps a legitimately-empty completion distinct from
    a cancel. No session → call inline."""
    from aiforge_core.runtime import chat_cancel
    if session_id is None:
        return complete_fn(role, convo)
    import threading as _th
    sem = _gen_sem()
    # Acquire a generation slot (cancellable wait). At the cap, a fresh
    # generation blocks until a prior (possibly abandoned) one finishes.
    while not sem.acquire(timeout=0.2):
        if chat_cancel.is_cancelled(session_id):
            return _CANCELLED

    box: dict = {}
    ev = _th.Event()             # per-call abort signal for the client HTTP layer

    def _call():
        # Bind the cancel token on THIS thread so the LLM client's HTTP layer
        # aborts the in-flight request the instant Stop fires (true model-
        # reclaim, not just abandoning the thread). Best-effort — a stub
        # complete_fn that never reaches the client is simply unaffected.
        try:
            from aiforge_core.llm import client as _client
            _client.set_cancel_event(ev)
        except Exception:  # noqa: BLE001
            pass
        try:
            box["out"] = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001 — surfaced on the main thread
            box["err"] = exc
        finally:
            sem.release()        # free the slot when the call REALLY finishes

    t = _th.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        if chat_cancel.is_cancelled(session_id):
            ev.set()             # abort the in-flight HTTP request
            return _CANCELLED    # slot frees when the (now-aborting) request ends
        t.join(timeout=0.2)
    # The request may have been aborted just as it finished — treat any
    # post-loop cancel as a cancel, not an error.
    if chat_cancel.is_cancelled(session_id):
        return _CANCELLED
    if "err" in box:
        raise box["err"]
    return box.get("out")


def _cave_mode() -> bool:
    """Cave mode = leanest useful context (smaller repo map, skip optional
    skills/workflows/mentions blocks, fewer memory hits, tighter condense
    budget). Env AIFORGE_CAVE_MODE wins; else the runtime setting."""
    env = os.environ.get("AIFORGE_CAVE_MODE")
    if env is not None:
        return env not in ("0", "false", "")
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get("cave_mode")) > 0
    except Exception:  # noqa: BLE001
        return False


def _compress_prompt(text: str) -> str:
    """Squeeze whitespace bloat out of the assembled prompt before it hits the
    LLM — dense context fits a small local window better and costs fewer tokens
    (the user's 'caveman'-style ask). SAFE/structural only: collapses runs of
    blank lines to one, strips trailing spaces, and drops consecutive duplicate
    lines. No words removed, no reordering — semantics unchanged. Off with
    AIFORGE_CHAT_COMPRESS_PROMPT=0."""
    if os.environ.get("AIFORGE_CHAT_COMPRESS_PROMPT", "1") in ("0", "false"):
        return text
    out: list[str] = []
    blanks = 0
    prev = None
    for raw in text.splitlines():
        ln = raw.rstrip()
        if not ln:
            blanks += 1
            if blanks > 1:
                continue          # collapse multiple blank lines to one
            out.append("")
            continue
        blanks = 0
        if ln == prev:
            continue              # drop an immediately-repeated line
        out.append(ln)
        prev = ln
    return "\n".join(out).strip()


def _ctx_budget_chars(role: str | None = None) -> int:
    """Char budget for the running conversation before auto-condensing. 0
    disables. Explicit override: AIFORGE_CHAT_CONTEXT_BUDGET_CHARS. Otherwise
    SIZED TO THE CONFIGURED MODEL WINDOW (context_window tokens → ~4 chars/token,
    keeping ~55% headroom for the system prompt + the model's own reply) instead
    of a fixed 48k that's too high for a small window and needlessly low for a
    big one."""
    env = os.environ.get("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    win = 0
    # Per-model context window (registry) for this role wins over the global.
    if role:
        try:
            from aiforge_core.config import model_registry
            win = int(model_registry.context_window_for_role(role))
        except Exception:  # noqa: BLE001
            win = 0
    if win <= 0:
        try:
            from aiforge_core.config import runtime_settings
            win = int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            win = 0
    # Cave mode condenses sooner — keep far less of the running history.
    headroom = 0.30 if _cave_mode() else 0.55
    if win > 0:
        return int(win * 4 * headroom)
    return 24000 if _cave_mode() else 48000


def _compact_convo(convo: list[dict], *, keep_recent: int = 18, role: str | None = None) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — no extra LLM call, so
    it's cheap and runs every turn. The agent can re-read files / ask the user
    if it needs detail from before the condense point."""
    budget = _ctx_budget_chars(role)
    if budget <= 0:
        return convo
    # Scale the verbatim tail to the budget: on a SMALL window, keeping 18 turns
    # could itself exceed the budget (condense fires but can't get under it).
    # ~2k chars/turn heuristic, floor 4 so there's always a usable recent slice.
    keep_recent = max(4, min(keep_recent, budget // 2000))
    if len(convo) <= keep_recent + 2:
        return convo
    if sum(len(m.get("content") or "") for m in convo) <= budget:
        return convo
    tail = convo[-keep_recent:]
    middle = convo[1:-keep_recent]
    if not middle:
        return convo
    tools: list[str] = []
    user_asks: list[str] = []
    finals: list[str] = []
    for m in middle:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "assistant":
            mt = _ACTION_RE.search(content)
            if mt:
                tools.append(mt.group(1))
            # An assistant FINAL (no ACTION:) is a substantive outcome — keep a
            # short trace so the summary carries decisions, not just tool counts.
            elif content and "ACTION:" not in content:
                finals.append(content.replace("\n", " ")[:160])
        elif role == "user" and content and not content.startswith("OBSERVATION:"):
            user_asks.append(content.replace("\n", " ")[:120])
    import collections as _c
    used = ", ".join(f"{t}×{n}" for t, n in _c.Counter(tools).most_common(8)) \
        or "discussion + reads"
    # ROLLING summary: carry forward asks/outcomes from the PRIOR breadcrumb
    # (if any) and merge with this window's, so a second+ condense doesn't drop
    # the original thread. Capped slices ([-N:]) keep it bounded — no growth.
    prior = convo[0].get("content") or ""
    prior_block = re.search(
        re.escape(_CONDENSE_OPEN) + r"(.*?)" + re.escape(_CONDENSE_CLOSE),
        prior, flags=re.S)
    if prior_block:
        ptext = prior_block.group(1)
        pa = re.search(r"Earlier asks: (.+)", ptext)
        po = re.search(r"Earlier outcomes: (.+)", ptext)
        if pa:
            user_asks = [s.strip() for s in pa.group(1).split(" · ")] + user_asks
        if po:
            finals = [s.strip() for s in po.group(1).split(" · ")] + finals
    # Rolling SUMMARY of the dropped middle — earlier asks + outcomes, not just
    # tool counts — so condensation doesn't erase what was discussed/decided
    # (the agent stops "forgetting" the thread after a long session). Heuristic,
    # no extra LLM call.
    summary_bits: list[str] = []
    if user_asks:
        summary_bits.append("Earlier asks: " + " · ".join(user_asks[-6:]))
    if finals:
        summary_bits.append("Earlier outcomes: " + " · ".join(finals[-4:]))
    summary = ("\n" + "\n".join(summary_bits)) if summary_bits else ""
    # Wrap the breadcrumb in a unique sentinel so the next condense can strip
    # exactly THIS block (not a look-alike phrase a rule/skill might contain).
    note = (f"{_CONDENSE_OPEN}\n"
            "[earlier conversation auto-condensed to fit the context window — "
            f"{len(middle)} messages omitted. Work done so far: {used}.{summary}\n"
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


def _build_repo_map(cwd: str, max_entries: int = 160, max_depth: int = 3) -> str:
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
                        f"prior sessions did):\n{body[:1800]}")
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

    # Latest user message drives mentions (#4) + microagent triggers (#6) +
    # memory recall. In simple/plan mode the API augments the last user turn
    # with an "[Interpreted request …]" enhancer block; key off the user's RAW
    # words (split that marker off) so recall/skills/mentions aren't diluted by
    # the boilerplate + restatement.
    last_user = next(
        (m.get("content") for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")
    last_user = last_user.split("\n\n---\n[Interpreted request")[0].strip() or last_user

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    cave = _cave_mode()
    rules = _rules_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    if plan_mode:                   # plan banner second — constrains this turn
        sys_msg = _PLAN_BANNER + "\n\n" + sys_msg
    # Cave mode: a much smaller repo map (the agent still has find/grep/list).
    sys_msg += "\n\n" + _repo_context(cwd) + "\n\n" + \
        _build_repo_map(cwd, max_entries=(60 if cave else 160),
                        max_depth=(2 if cave else 3))
    # Skills / workflows / @-mentions — OPTIONAL context blocks. Cave mode skips
    # them (the agent can still skill_search / workflow_search on demand) to keep
    # the prompt lean.
    if not cave:
        try:
            from aiforge_core.runtime import skills as _skills
            sk_block = _skills.auto_context(last_user, cwd)
            if sk_block:
                sys_msg += "\n\n" + sk_block
        except Exception:  # noqa: BLE001
            pass
        try:
            from aiforge_core.runtime import workflows as _workflows
            wf_block = _workflows.auto_context(last_user, cwd)
            if wf_block:
                sys_msg += "\n\n" + wf_block
        except Exception:  # noqa: BLE001
            pass
        try:
            from aiforge_core.runtime import mentions as _mentions
            ment_block, _toks = _mentions.expand(last_user, cwd)
            if ment_block:
                sys_msg += "\n\n" + ment_block
        except Exception:  # noqa: BLE001
            pass
    # Self-learning recall — EVERY turn, keyed to the CURRENT user message. Cave
    # mode pulls fewer hits.
    recall = _memory_recall(cwd, last_user, limit=(3 if cave else 6))
    if recall:
        sys_msg += "\n\n" + recall
    # SESSION IMAGES: descriptions of images the user attached, so the (maybe
    # text-only) model can answer questions about them all session long.
    _img_blocks: list[dict] = []
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_media
            _img_ctx = chat_media.context_block(session_id)
            if _img_ctx:
                sys_msg += "\n\n" + _img_ctx
            _img_blocks = chat_media.image_blocks_for_turn(session_id, role)
        except Exception:  # noqa: BLE001 — images must never break a turn
            _img_blocks = []
    sys_msg = _compress_prompt(sys_msg)   # trim whitespace bloat (caveman-style)
    convo: list[dict] = [{"role": "system", "content": sys_msg}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})
    # When the model is vision-capable, fold the actual images into the latest
    # user turn (multimodal content) so it can SEE them, not just their text.
    if _img_blocks:
        for _m in reversed(convo):
            if _m.get("role") == "user":
                _m["content"] = [{"type": "text", "text": _m.get("content") or ""},
                                 *_img_blocks]
                break

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
        convo = _compact_convo(convo, role=role)
        if len(convo) < _before and not condensed_notified:
            condensed_notified = True   # notify ONCE, not every over-budget turn
            yield {"type": "thought", "role": "system",
                   "text": "⚙ condensed earlier context to stay within the window"}
        # M3: surface how full the context window is (char-estimate; ~4 chars/
        # token) so the user can see they're approaching the condense point.
        _ctx_chars = sum(len(m.get("content") or "") for m in convo)
        _ctx_budget = _ctx_budget_chars(role)
        if _ctx_budget > 0:
            yield {"type": "usage", "context_chars": _ctx_chars,
                   "budget_chars": _ctx_budget,
                   "pct": min(100, round(_ctx_chars * 100 / _ctx_budget))}
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "text": f"llm error: {exc}"}
            yield {"type": "done"}
            return
        # H1: Stop pressed DURING generation — the cancellable wrapper returned
        # the sentinel (the abandoned LLM call finishes in the background,
        # ignored). Distinct from a legitimately-empty completion below.
        if out is _CANCELLED:
            yield {"type": "error", "text": "stopped by user"}
            yield {"type": "done"}
            return
        if out is None:
            out = ""   # a real empty completion — treat as an empty turn

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
        _force_review = (session_id is not None and _is_mutating(name, args)
                         and chat_approve.review_edits(session_id))
        # Destructive delete (rm -rf, etc): the run_command tool has its OWN
        # confirm_delete arg gate (delete_guard). If we don't route it through
        # the approval gate AND mark it confirmed on approve, the tool keeps
        # refusing ("re-issue with confirm_delete=true") and the model loops
        # asking the user to "type yes" forever. So always gate it, and let the
        # human's Approve BE the confirmation.
        _destructive_del = False
        # Captured-rule "never re-ask" flags — set ONLY by an EXPLICIT user
        # opt-in (rule_capture.set_gate_flag), never by the classifier. A
        # commit_auto_approve flag auto-approves a whole-command git commit/add/
        # push; allow_delete auto-confirms a destructive delete — for the scope
        # (session → repo precedence; autonomous runs ignore chat-set flags).
        _auto_commit = False
        if name in ("run_command", "bash", "run_shell", "shell", "serve"):
            _cmd = args.get("cmd") or args.get("command") or ""
            try:
                from aiforge_core.runtime.tools import delete_guard
                _destructive_del = (not delete_guard.allow_delete(
                    ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
                    and delete_guard.is_destructive_delete(_cmd))
            except Exception:  # noqa: BLE001
                _destructive_del = False
            try:
                from aiforge_core.runtime import rule_capture as _rc
                _repo = _repo_name(cwd)
                if _rc.is_commit_command(_cmd) and _rc.flag_active(
                        "commit_auto_approve", repo=_repo, session_id=session_id):
                    _auto_commit = True
                if _destructive_del and _rc.flag_active(
                        "allow_delete", repo=_repo, session_id=session_id):
                    _destructive_del = False
                    args["confirm_delete"] = True
            except Exception:  # noqa: BLE001
                pass
        _gate = (verdict["policy"] == tool_policy.ASK or _force_review
                 or _destructive_del)
        # A captured "commit directly" flag may auto-approve the gate ONLY when
        # the SOLE reason to gate is a pure whole-command git commit/add/push —
        # NEVER when a destructive delete (or any non-commit risk: forced review,
        # DENY) co-occurs. So `git commit && rm -rf` is NOT auto-approved.
        if _gate and _auto_commit and not _destructive_del and not _force_review \
                and verdict["policy"] != tool_policy.DENY:
            _gate = False
            # Audit: emit an attributable record of the bypass (not invisible).
            try:
                from aiforge_core.runtime import rule_capture as _rc2
                _ascope = _rc2.flag_active_scope(
                    "commit_auto_approve", repo=_repo_name(cwd),
                    session_id=session_id)
            except Exception:  # noqa: BLE001
                _ascope = None
            yield {"type": "auto_approved", "name": name,
                   "flag": "commit_auto_approve", "scope": _ascope}
        if _gate:
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
            # M4: a gate left unanswered (user navigated away) auto-rejects on
            # timeout — surface it explicitly so the UI shows "approval expired"
            # instead of silently moving on with a rejected action.
            if decision.get("note") == "approval timed out":
                yield {"type": "approval_expired", "id": seq, "name": name}
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
