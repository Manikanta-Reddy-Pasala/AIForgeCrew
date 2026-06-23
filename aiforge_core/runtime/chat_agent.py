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
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if glob and not f.endswith(glob.lstrip("*")):
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
}

_SYSTEM = """You are AIForge, an autonomous coding assistant with FULL access to \
the user's filesystem and shell in the working directory {cwd}.

You work by emitting ONE step at a time in this exact text format.

To use a tool:
THOUGHT: <your reasoning>
ACTION: <one of: file_read, file_write, file_create, file_patch, list_dir,
         find, grep, run_command, ensure_runtime, project, remember_rule,
         memory_lookup, memory_write>
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

When you are done and ready to reply to the user:
THOUGHT: <reasoning>
FINAL: <your full natural-language answer>

When the request is ambiguous, you're missing information, or you'd \
otherwise have to guess or keep retrying the same thing, ASK the user \
instead of circling:
THOUGHT: <why you need input>
ASK: <one concise, specific question>
The turn ends and the user's next message answers you.

Rules: emit exactly one ACTION or one FINAL per turn. After each ACTION you \
receive an OBSERVATION with the tool result, then continue. Keep going until \
the task is complete, then give FINAL. Do real work — read and edit files, run \
commands — rather than guessing.

Operating principles — be fully autonomous, don't stop half-way:
- ASK, don't circle: if you're unsure what the user wants, lack a needed \
detail, or catch yourself repeating a step that isn't working, emit ASK: \
<question> and wait — never loop on the same failing action or guess at an \
ambiguous request.
- RULE BOOK: when the user says "remember…", "always…", "never…", "for \
all sessions", or states a standing rule about the folder/repo/workflow, \
immediately call remember_rule (scope=repo for this repo, scope=global for \
everywhere). Any RULES shown above are user rules — always obey them.
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
) -> Iterator[dict]:
    """Drive the ReAct loop until the agent finishes or a stuck loop is
    detected (NOT a step count). Yields SSE-ready event dicts:

    ``{"type": "thought", "text"}`` · ``{"type": "tool", "name", "args",
    "result"}`` · ``{"type": "message", "text"}`` (final) ·
    ``{"type": "error", "text"}`` · ``{"type": "done"}``.
    """
    if complete_fn is None:
        from aiforge_core.llm.client import complete as complete_fn  # type: ignore

    from aiforge_core.runtime import chat_cancel
    chat_cancel.set_active(session_id)

    import collections
    safety = max_steps or int(os.environ.get("AIFORGE_CHAT_SAFETY_CAP", "2000"))

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    rules = _rules_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    sys_msg += "\n\n" + _repo_context(cwd) + "\n\n" + _build_repo_map(cwd)
    convo: list[dict] = [{"role": "system", "content": sys_msg}]
    for m in messages:
        r = m.get("role") or "user"
        convo.append({"role": "assistant" if r == "assistant" else "user",
                      "content": m.get("content") or ""})

    action_counts: dict[str, int] = {}
    recent_outputs: collections.deque = collections.deque(maxlen=_OUTPUT_REPEAT)

    n = 0
    while n < safety:
        n += 1
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            yield {"type": "error", "text": "stopped by user"}
            yield {"type": "done"}
            return
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
        fn = TOOLS.get(name)
        if fn is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            try:
                result = fn(args, cwd)
            except KeyError as exc:
                result = {"ok": False, "error": f"missing arg: {exc}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        obs = json.dumps(result)[:_MAX_OBS]
        convo.append({"role": "user", "content": f"OBSERVATION: {obs}"})

    yield {"type": "message",
           "text": "(stopped: hit the runaway safety cap — "
                   "raise AIFORGE_CHAT_SAFETY_CAP if this was real work)"}
    yield {"type": "done"}
