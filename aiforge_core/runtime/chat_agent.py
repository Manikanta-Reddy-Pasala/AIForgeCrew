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
# Content-READ tools return a document the model must see IN FULL to work with
# (a long Confluence page, a Jira issue, a file). The generic 6k cap truncated
# them mid-page — the model then reported "API truncation" and gave up. Give
# these a much larger observation budget. Tunable via env.
try:
    _MAX_OBS_READ = max(_MAX_OBS, int(os.environ.get("AIFORGE_CHAT_MAX_OBS_READ",
                                                     "80000")))
except (TypeError, ValueError):
    _MAX_OBS_READ = 80000
_READ_OBS_TOOLS = frozenset({
    "confluence_read", "confluence_spaces", "confluence_page_by_title",
    "confluence_labels", "confluence_comments", "confluence_descendants",
    "jira_read", "jira_worklog", "jira_projects", "jira_remote_links",
    "context_gather", "resolve_repo", "jira_resolve_project",
    "confluence_resolve_space",
    "jira_boards", "jira_sprints", "jira_sprint_issues", "jira_dashboards",
    "jira_dashboard_read", "jira_myself", "file_read", "read_lines",
    "gitlab_read", "web_fetch", "web_crawl", "email_read",
})


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
    from aiforge_core.runtime import request_context
    raw = request_context.get_workspace_dir()
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
    # A directory passed to file_read used to just error ("not a file"), which
    # reads as nonsense when the model is exploring. Return its LISTING instead
    # (same shape as list_dir) so the mistake is still useful navigation.
    if p.is_dir():
        try:
            entries = [(c.name + "/") if c.is_dir() else c.name
                       for c in sorted(p.iterdir())]
        except OSError as exc:
            return {"ok": False, "error": f"cannot list dir {args['path']}: {exc}"}
        return {"ok": True, "is_dir": True, "path": str(p), "entries": entries,
                "note": "this is a DIRECTORY — listing its entries; "
                        "call file_read on a specific file to read contents"}
    if not p.is_file():
        return {"ok": False, "error": f"no such file or directory: {args['path']}"}
    return {"ok": True, "content": p.read_text(encoding="utf-8", errors="replace")}


# Code extensions where a syntax check is meaningful (so we never reject a
# legit prose/data file for unbalanced braces). The guard is brace-balance for
# most, compile() for .py.
_SYNTAX_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
                ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".php",
                ".rb", ".swift", ".scala")


def _syntax_check(path: str, content: str, args: dict) -> "str | None":
    """Return an error string if ``content`` is broken code, else None. Only
    runs for known code extensions, skips empty files, and honours force:true."""
    if args.get("force") or not content.strip():
        return None
    if not str(path).lower().endswith(_SYNTAX_EXTS):
        return None
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(path, content)
        return None if ok else err
    except Exception:  # noqa: BLE001 — never let the guard break a write
        return None


def _t_file_write(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    content = args.get("content", "")
    bad = _syntax_check(str(p), content, args)
    if bad:
        return {"ok": False, "error": "syntax_invalid", "detail": bad,
                "hint": "fix the syntax, or pass force:true to write anyway"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(content)}


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
    new_body = body.replace(old, args["new_text"], 1)
    bad = _syntax_check(str(p), new_body, args)
    if bad:
        return {"ok": False, "error": "syntax_invalid", "detail": bad,
                "hint": "the edit would break the file; fix it or pass force:true"}
    p.write_text(new_body, encoding="utf-8")
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


_SCRIPT_RUNNERS = {"bash", "sh", "zsh", "python", "python3", "node", "ruby",
                   "perl", "uv"}
_SCRIPT_EXTS = (".sh", ".bash", ".py", ".js", ".mjs", ".rb", ".pl")


def _preflight_missing_path(cmd: str, base: str) -> str | None:
    """Cheap existence check BEFORE running: a `cd <dir>` into a folder that
    doesn't exist, or `bash <script>` / `./script.sh` on a missing file, fails
    with a cryptic shell error the model then thrashes on. Validate LITERAL
    paths only — anything dynamic ($VAR, ``, $(), globs) is skipped (fail-open).
    Tracks `cd` chains so `cd a && ./b.sh` checks b.sh under a/. Returns a
    human-actionable error string, or None when nothing is provably missing."""
    import shlex
    cur = base
    # split on the common separators; ignore parse errors (complex shell → skip)
    for seg in re.split(r"&&|\|\||;|\n|\|", cmd or ""):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue

        def _literal(p: str) -> bool:
            return not any(ch in p for ch in ("$", "`", "*", "?", "(", "{"))

        head = toks[0]
        if head == "cd" and len(toks) >= 2:
            tgt = toks[1]
            if tgt == "-" or not _literal(tgt):
                return None            # dynamic — stop tracking, fail open
            d = os.path.expanduser(tgt)
            d = d if os.path.isabs(d) else os.path.join(cur, d)
            if not os.path.isdir(d):
                return (f"cd target does not exist: {tgt!r} (resolved "
                        f"{os.path.normpath(d)}). Nothing was run. Check the "
                        "path first (list_dir / file_find) and re-issue with "
                        "the real folder.")
            cur = os.path.normpath(d)
            continue
        script = None
        if head in _SCRIPT_RUNNERS:
            cand = next((t for t in toks[1:] if not t.startswith("-")), None)
            if cand and cand.lower().endswith(_SCRIPT_EXTS):
                script = cand
        elif (head.startswith("./") or os.path.isabs(head)) \
                and head.lower().endswith(_SCRIPT_EXTS):
            script = head
        if script and _literal(script):
            p = os.path.expanduser(script)
            p = p if os.path.isabs(p) else os.path.join(cur, p)
            if not os.path.isfile(p):
                return (f"script does not exist: {script!r} (resolved "
                        f"{os.path.normpath(p)}). Nothing was run. Find it "
                        "first (file_find / list_dir) or create it, then "
                        "re-issue.")
    return None


_OBS_TEXT_KEYS = ("content", "text", "body", "markdown", "preview", "stdout")


def _smart_truncate_obs(result, cap: int) -> str:
    """Serialize a tool result to at most ``cap`` chars for the OBSERVATION.

    A plain ``json.dumps(result)[:cap]`` slices mid-sentence/mid-JSON —
    the model reads a broken tail and mis-handles long files/pages. When a
    content-read result exceeds the cap, cut its LARGEST text field at a
    STRUCTURE boundary (chonkie RecursiveChunker when installed) with an
    explicit continuation note, so the model knows the doc continues and
    how to get more. Falls back to the old blunt slice on any failure."""
    try:
        raw = json.dumps(result)
    except (TypeError, ValueError):
        raw = json.dumps(str(result))
    if len(raw) <= cap:
        return raw
    try:
        if isinstance(result, dict):
            key = max((k for k in _OBS_TEXT_KEYS
                       if isinstance(result.get(k), str)),
                      key=lambda k: len(result[k]), default=None)
            if key and len(result[key]) > (len(raw) - cap):
                from aiforge_core.integrations import chonkie_text_adapter
                text = result[key]
                budget = max(500, len(text) - (len(raw) - cap) - 200)
                if chonkie_text_adapter.available():
                    kept = chonkie_text_adapter.cut_at_structure(text, budget)
                else:
                    # dep-free structural fallback: last paragraph boundary
                    kept = text[:budget]
                    nl = kept.rfind("\n\n")
                    if nl > budget // 2:
                        kept = kept[:nl]
                trimmed = dict(result)
                trimmed[key] = (kept + f"\n…[TRUNCATED at a structure "
                                f"boundary — {len(kept)} of {len(text)} chars "
                                "shown. The document CONTINUES: use read_lines "
                                "with an offset, or ask for a specific "
                                "section.]")
                out = json.dumps(trimmed)
                if len(out) <= cap + 400:      # small tolerance for the note
                    return out
    except Exception:  # noqa: BLE001 — smart cut is best-effort
        pass
    return raw[:cap]


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
    # Pre-flight: a literal `cd <missing dir>` or `bash <missing script>` is
    # refused with an actionable error instead of the shell's cryptic
    # "No such file or directory" (which the model then thrashes on).
    _missing = _preflight_missing_path(cmd, base)
    if _missing:
        return {"ok": False, "blocked": "missing_path", "error": _missing}
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
            # Capture whatever the command buffered BEFORE we kill it, so the
            # agent sees partial output (e.g. which tests ran/passed before the
            # hang) and can adapt — instead of a blind "timeout" with no signal.
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                _kill_proc(proc)
                out, err = "", ""
            return {"ok": False, "timed_out": True, "code": None,
                    "stdout": (out or "")[-_MAX_OBS:],
                    "stderr": (err or "")[-_MAX_OBS:],
                    "error": f"timed out after {timeout}s — PARTIAL output "
                    "above. This is not a failure of your change: the command "
                    "just ran longer than the limit. Next: run a NARROWER "
                    "command (one test file or a single test case), or re-issue "
                    "this exact command with a larger \"timeout\" (e.g. 600). Do "
                    "NOT undo your edits over a timeout."}
        _time.sleep(0.2)
    # Bound communicate(): a daemon grandchild inheriting the stdout pipe
    # (e.g. `npm run dev &`) keeps it open after the process exits, so an
    # un-timed communicate() blocks forever even past the deadline.
    try:
        _ct = int(os.environ.get("AIFORGE_COMMUNICATE_TIMEOUT_S", "10"))
    except (TypeError, ValueError):
        _ct = 10
    try:
        out, err = proc.communicate(timeout=_ct)
    except subprocess.TimeoutExpired:
        _kill_proc(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out, err = "", ""
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


_GIT_TOPLEVEL_CACHE: dict[str, str | None] = {}


def _git_toplevel(cwd: str | None) -> str | None:
    """Repo root for ``cwd`` (``git rev-parse --show-toplevel``), cached and
    soft-failing to None outside a work tree. Lets a SUBDIR resolve the same
    repo key as the root (gap M3)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GIT_TOPLEVEL_CACHE:
        return _GIT_TOPLEVEL_CACHE[key]
    top: str | None = None
    try:
        out = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            top = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — recall must never break on git
        top = None
    _GIT_TOPLEVEL_CACHE[key] = top
    return top


def _chat_repo_key(cwd: str | None) -> str:
    """Repo key for chat recall — resolves the GIT-TOPLEVEL basename (so a
    subdir recalls the same repo as the root), falling back to the raw cwd
    basename, then ``AIFORGE_AFM_REPO``, then the literal ``"repo"`` (gap M3).
    Note ``repo_key`` is always truthy for a real path, so its ``or env``
    fallback was dead — we chain the env explicitly here."""
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")


def _t_memory_lookup(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.memory import unified_query as _uq
        # F2/M3: scope recall to the SAME repo the chat WRITE path files under
        # (git-toplevel basename), so chat's own facts aren't filtered out.
        _repo = _chat_repo_key(cwd)
        res = _uq.query(args["query"], limit=int(args.get("limit", 6)),
                        repo=_repo)
        return {"ok": True, "hits": [
            {"text": (h.get("text") or "")[:400], "source": h.get("source")}
            for h in res.get("hits", [])
        ]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_search_chat_sessions(args: dict, cwd: str) -> dict:
    """Search PRIOR chat sessions' message content — recall what you discussed
    with the user in past conversations. Local + cheap (one SQLite scan)."""
    try:
        q = args.get("query") or args.get("q") or ""
        limit = _coerce_int(args.get("limit"), 6)
        from aiforge_core.runtime import chat_store
        return {"ok": True, "hits": chat_store.search_messages(q, limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_memory_write(args: dict, cwd: str) -> dict:
    """Persist a durable fact/decision into the knowledge memory so future
    chats + tickets recall it. repo defaults to the working dir's name."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        # Use the SAME git-toplevel repo key the recall path uses
        # (_chat_repo_key), so a fact written from a SUBDIRECTORY chat is
        # filed under the repo the later recall queries — otherwise a subdir
        # write lands under the subdir basename and is never recalled.
        repo = args.get("repo") or _chat_repo_key(cwd) or "chat"
        # scope="global" writes a repo-less fact recalled across EVERY ticket/
        # page/repo (general knowledge); default scope keeps it to THIS context.
        _scope = (args.get("scope") or "").lower()
        return _mw(
            text=args["text"],
            kind=args.get("kind", "note"),
            tags=list(args.get("tags") or []) + ["chat"],
            decision=bool(args.get("decision")),
            repo=repo,
            scope=_scope,
        )
    except KeyError:
        return {"ok": False, "error": "missing arg: text"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_create_job_script(args: dict, cwd: str) -> dict:
    """JOB-BUILDER finalize: write the approved script to the local
    ~/.aiforge/jobs folder and register a cron job that RUNS it (deterministic
    — no ticket, no LLM per fire). Args: name, cron, script, optional
    description. Mirrors POST /api/jobs/script so the chat builder can finalize
    in-conversation."""
    try:
        name = str(args.get("name") or "").strip()
        cron = str(args.get("cron") or "").strip()
        script = str(args.get("script") or "")
        if not name or not cron or not script.strip():
            return {"ok": False, "error": "need name, cron, and script"}
        from aiforge_core.jobs import parse as jobs_parse
        from aiforge_core.jobs import scripts as jobs_scripts
        from aiforge_core.jobs import store as jobs_store
        if not jobs_parse.schedulable(cron):
            return {"ok": False,
                    "error": f"invalid or unschedulable cron: {cron!r}"}
        path = jobs_scripts.write_script(name, script)
        # TEST BEFORE SCHEDULE: run the script once. A wrong JQL/filter would
        # otherwise be scheduled as-is and fire forever doing nothing. On
        # failure, DON'T schedule and DON'T leave an orphan script. `skip_test`
        # (default off) is the escape for destructive/time-sensitive scripts.
        trial_output = None
        if not bool(args.get("skip_test")):
            trial = jobs_scripts.run_script(path)
            if not trial.get("ok"):
                jobs_scripts.delete_script(path)
                return {"ok": False, "tested": True,
                        "error": ("trial run FAILED (exit "
                                  f"{trial.get('returncode')}) — job NOT "
                                  "scheduled. Fix the script and retry.\n"
                                  f"STDOUT:\n{trial.get('stdout', '')}\n"
                                  f"STDERR:\n{trial.get('stderr', '')}")}
            trial_output = trial.get("stdout")
        # DEDUPE: replace any existing job(s) with the same name (+ their
        # script files) instead of piling up duplicates that all fire.
        replaced = []
        try:
            for j in jobs_store.list_jobs():
                if str(j.get("name") or "").strip().lower() == name.lower():
                    sp = j.get("script_path")
                    if sp and sp != path and jobs_scripts.is_within_jobs_dir(sp):
                        jobs_scripts.delete_script(sp)
                    jobs_store.delete(j["id"])
                    replaced.append(j["id"])
        except Exception:  # noqa: BLE001 — dedupe is best-effort, never block create
            pass
        nxt = jobs_parse.next_runs(cron, n=1)[0]
        job = jobs_store.create(
            name=name, cron=cron, ticket_title=name,
            ticket_body=(str(args.get("description") or "").strip()
                         or f"Runs script: {path}"),
            next_run_at=nxt, kind="script", script_path=path)
        return {"ok": True, "job_id": job["id"], "script_path": path,
                "human_schedule": jobs_parse.human_schedule(cron),
                "next_run_at": job["next_run_at"],
                "tested": not bool(args.get("skip_test")),
                "trial_output": trial_output,
                "replaced_jobs": replaced}
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


# Elaboration prompts — turn a user's rough input into a well-structured
# playbook BODY (no frontmatter; write_skill/write_workflow add that). Local
# models often emit a thin one-liner as the body; running it through the model
# once server-side guarantees a formatted, elaborated artifact.
_ELABORATE_PROMPT = {
    "skill": ("Rewrite the following into a clear, reusable SKILL body: a short "
              "intro line then concise numbered/bulleted steps the agent "
              "follows. Keep the user's intent; add the obvious missing detail. "
              "Output ONLY the markdown body — NO YAML frontmatter, no name."),
    "workflow": ("Rewrite the following into a WORKFLOW body: numbered "
                 "end-to-end steps, each concrete and in dependency order, with "
                 "a final done-check. Keep the user's intent; fill obvious gaps. "
                 "Output ONLY the markdown body — NO frontmatter."),
    "rule": ("Rewrite the following into a coding RULE: a '# Title' line then "
             "tight imperative bullet points the agent must follow. Keep the "
             "intent; make each bullet testable. Output ONLY the markdown."),
}


def _elaborate_body(kind: str, body: str, *, name: str = "",
                    description: str = "") -> str:
    """Format+elaborate a rough ``body`` via the model. Best-effort: returns the
    ORIGINAL body on any failure/empty, and skips when disabled or the body is
    already substantial (>= 400 chars with structure) so we don't over-rewrite a
    good doc. Off with AIFORGE_BUILDER_ELABORATE=0."""
    body = (body or "").strip()
    if os.environ.get("AIFORGE_BUILDER_ELABORATE", "1") in ("0", "false", "no"):
        return body
    prompt = _ELABORATE_PROMPT.get(kind)
    if not prompt or not body:
        return body
    # Already a structured, non-trivial doc → leave it (avoid churn).
    if len(body) >= 400 and ("\n" in body) and any(
            m in body for m in ("- ", "1.", "# ", "* ")):
        return body
    ctx = (f"Name: {name}\n" if name else "") + \
          (f"Purpose: {description}\n" if description else "")
    try:
        from aiforge_core.llm import client as _llm
        out = _llm.complete("architect", [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (ctx + "\nInput:\n" + body).strip()},
        ], max_tokens=900, temperature=0.2,
            timeout_s=int(os.environ.get("AIFORGE_BUILDER_ELABORATE_TIMEOUT_S", "45")))
        out = (out or "").strip()
        # Strip a stray ```markdown fence if the model wrapped it.
        if out.startswith("```"):
            parts = out.split("```")
            if len(parts) >= 2:
                out = parts[1]
                if out.lower().lstrip().startswith("markdown"):
                    out = out.lstrip()[8:]
                out = out.strip()
        return out or body
    except Exception:  # noqa: BLE001 — elaboration is best-effort, never block save
        return body


def _t_remember_rule(args: dict, cwd: str) -> dict:
    """Persist a user rule that must apply to EVERY future session. Writes to
    the SAME global rules store the Library UI lists/creates/deletes
    (``repo_rules`` → ~/.aiforge/rules/) so a rule built in chat shows up in the
    Library — and is injected every turn by ``_rules_context``."""
    try:
        from aiforge_core.runtime import repo_rules
        text = (args.get("text") or args.get("rule") or args.get("body")
                or "").strip()
        if not text:
            return {"ok": False, "error": "missing 'text'"}
        # Derive a stable name from an explicit arg or the first line of text
        # (repo_rules keys the file by a slug of the name).
        name = (args.get("name") or "").strip()
        if not name:
            first = text.lstrip("-# ").splitlines()[0] if text else "rule"
            name = re.sub(r"\s+", " ", first).strip()[:60] or "rule"
        globs = args.get("globs")
        if isinstance(globs, str):
            globs = [g.strip() for g in globs.split(",") if g.strip()]
        # Unified artifact frontmatter (same shape as skills/workflows):
        # name / description / triggers / scope.
        description = (args.get("description") or "").strip()
        triggers = args.get("triggers")
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        scope = (args.get("scope") or "global").lower()
        text = _elaborate_body("rule", text, name=name,
                               description=description)   # LLM format+elaborate
        res = repo_rules.write_rule(
            name, text, globs=globs, always=True,
            description=description, triggers=triggers, scope=scope)
        if not res.get("ok"):
            return res
        # Also record in knowledge memory so unified_query / recall surface the
        # rule alongside facts. scope=repo → tag to THIS repo; scope=global →
        # repo-agnostic (repo=None; recall unions NULL-repo rows so it applies
        # everywhere). Write directly to the embedded store — memory_write
        # refuses a null repo, which would silently drop global rules.
        scope = (args.get("scope") or "global").lower()
        try:
            from aiforge_core.memory import backend_select as _bsel
            if _bsel.embedded():
                from aiforge_core.memory import sqlite_memory as _sqlmem
                _sqlmem.write_unit(
                    text=f"RULE: {text}", kind="note", source="rule",
                    tags=["rule", scope],
                    repo=(_chat_repo_key(cwd) if scope == "repo" else None))
        except Exception:  # noqa: BLE001 — memory write must not block the rule
            pass
        return {"ok": True, "name": name, "scope": scope, "remembered": text,
                "path": res.get("path")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_BULLET_TRIGGERS_RE = re.compile(r"^\[triggers:\s*([^\]]*)\]\s*(.*)$")


def _parse_bullet(line: str) -> tuple[tuple[str, ...], str]:
    """Strip a leading '- ' and an optional '[triggers: a, b]' prefix.
    Returns (triggers, text). No triggers prefix → triggers=() (always-on,
    backward compatible with every bullet written before this feature)."""
    text = line[2:] if line.startswith("- ") else line
    m = _BULLET_TRIGGERS_RE.match(text.strip())
    if not m:
        return (), text.strip()
    trig = tuple(t.strip().lower() for t in m.group(1).split(",") if t.strip())
    return trig, m.group(2).strip()


# `md_store._find_by_source` globs + parses EVERY file in the memory dir
# until it finds a frontmatter `source` match — called 2-3x per chat turn
# (`_rules_context` x2 + `_repo_context`), so it re-scans the whole memory
# dir on every single message with no caching at all. Cache POSITIVE hits
# only (never negative — a source with no file yet must keep being
# re-checked, since capture can create it moments later in the same turn);
# once a source's file exists its identity never changes (bullets are
# appended into the same file), so caching the path is always safe once
# found. `.exists()` on a hit is a cheap stat, far cheaper than the O(files)
# scan it replaces. Keyed by (memory_dir, source) — NOT source alone — so a
# changed AIFORGE_MEMORY_MD_DIR (tests reconfigure it per-case; a real
# deployment could reconfigure it too) can never serve a stale path from a
# now-irrelevant memory directory.
_source_path_cache: dict[tuple[str, str], Path] = {}


def _cached_find_by_source(source: str) -> Path | None:
    from aiforge_core.memory import md_store
    key = (str(md_store.memory_dir()), source)
    p = _source_path_cache.get(key)
    if p is not None:
        try:
            if p.exists():
                return p
        except Exception:  # noqa: BLE001 — a bad cache entry is a miss, not a crash
            pass
    p = md_store._find_by_source(source)
    if p is not None:
        _source_path_cache[key] = p
    return p


def _preferences_context(cwd: str) -> str:
    """The user's captured PREFERENCES (global defaults/conventions), injected
    every turn so a once-stated preference is always honoured and never
    re-asked. Stored as ``pref:``-tagged units by preference_capture; embedded
    backend only. Best-effort — never breaks the turn."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return ""
        import json as _json
        from aiforge_core.memory import sqlite_memory as _m
        lines: list[str] = []
        with _m._conn() as c:  # noqa: SLF001 — internal read, best-effort
            for r in c.execute(
                "SELECT text, tags FROM memory_units WHERE kind='preference' "
                "ORDER BY id DESC LIMIT 40").fetchall():
                try:
                    tags = _json.loads(r["tags"] or "[]") or []
                except (TypeError, ValueError):
                    tags = []
                if any(isinstance(t, str) and t.startswith("pref:") for t in tags):
                    txt = (r["text"] or "").strip().replace("\n", " ")
                    if txt:
                        lines.append(f"- {txt[:240]}")
        if not lines:
            return ""
        return ("USER PREFERENCES (standing defaults/conventions the user set — "
                "apply them without asking again):\n" + "\n".join(lines[:40]))
    except Exception:  # noqa: BLE001
        return ""


def _rules_context(cwd: str, query: str = "") -> str:
    """The user's persistent rule book (global + this-repo), injected into
    EVERY session so the rules are always honoured. Untagged bullets are
    always-on (legacy). Bullets tagged with an inline '[triggers: ...]'
    prefix are gated by relevance to ``query`` via the shared scorer; a
    near-tie among tagged bullets injects an ASK note instead of silently
    picking one."""
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import skills as _sk
        always_lines: list[str] = []
        tagged: list[_sk.Skill] = []
        # Align the repo rule key with the canonical recall key (_chat_repo_key,
        # git-toplevel) so a rule captured for this repo is read back under the
        # SAME key it was written — _repo_name (workspace/subdir basename) drifted.
        for src in ("rules:global", f"rules:{_chat_repo_key(cwd)}"):
            p = _cached_find_by_source(src)
            if p is None:
                continue
            body = md_store._parse(p).get("body", "")
            for line in body.splitlines():
                if not line.strip():
                    continue
                trig, text = _parse_bullet(line)
                if not trig:
                    always_lines.append("- " + text)
                else:
                    tagged.append(_sk.Skill(
                        name=text[:60], description="", triggers=trig,
                        body=text, source=src, always=False, priority=0))
        # ALSO read the repo_rules store — the SAME store the Library UI /
        # create-form / remember_rule write to. Use load_rules(cwd), which
        # merges builtin → global (~/.aiforge/rules) → repo-local and dedups BY
        # NAME with the most-specific winning: a REPO rule OVERRIDES a global
        # rule of the same name (parity with the team/pipeline path). An
        # always-on rule joins the always block; a glob-scoped rule becomes a
        # trigger-gated bullet so relevance scoring applies.
        try:
            from aiforge_core.runtime import repo_rules as _rr
            # load_rules needs a repo root; fall back to global-only when we're
            # not in a repo (still honours global rules).
            try:
                _rules = list(_rr.load_rules(cwd)) if cwd else list(
                    _rr.load_global_rules())
            except Exception:  # noqa: BLE001
                _rules = list(_rr.load_global_rules())
            for r in _rules:
                rt = (r.body or "").strip()
                if not rt:
                    continue
                if getattr(r, "always", True) or not getattr(r, "globs", None):
                    always_lines.append("- " + rt.replace("\n", " ")[:400])
                else:
                    tagged.append(_sk.Skill(
                        name=(r.name or rt[:60]), description="",
                        triggers=tuple(str(g).lower() for g in r.globs),
                        body=rt.replace("\n", " ")[:400], source="repo_rules",
                        always=False, priority=0))
        except Exception:  # noqa: BLE001 — repo_rules read is best-effort
            pass
        blocks: list[str] = list(always_lines)
        ambiguous_note = ""
        if tagged:
            # Scorer runs in its OWN guard: a select_or_ask defect must never
            # drop the legacy always-on `blocks` (the noise-fix turning into
            # a rules-vanish bug). On any scorer error, fail open — include
            # every tagged bullet, same as the no-query path.
            try:
                if query:
                    chosen, ambiguous = _sk.select_or_ask(
                        query, pool=tagged, k=len(tagged))
                    blocks.extend("- " + s.body for s in chosen)
                    if ambiguous:
                        names = " or ".join(f"'{s.body}'" for s in ambiguous[0])
                        ambiguous_note = (
                            "\nAMBIGUOUS RULE MATCH: " + names + " both matched"
                            " — ASK the user which applies before proceeding, "
                            "don't guess.")
                else:
                    # No query to score against (defensive) — fail open.
                    blocks.extend("- " + s.body for s in tagged)
            except Exception:  # noqa: BLE001 — fail open, keep always-on rules
                blocks.extend("- " + s.body for s in tagged)
        if not blocks:
            return ""
        # Rules are MANDATORY — never silently drop tail rules to a hard slice.
        # Cap is env-tunable and a truncation is called out so the agent knows
        # rules exist beyond the cut (and can look them up) instead of treating
        # the visible subset as complete.
        try:
            cap = max(400, int(os.environ.get("AIFORGE_RULES_MAX_CHARS", "4000")))
        except ValueError:
            cap = 4000
        body_txt = "\n".join(blocks)
        if len(body_txt) > cap:
            body_txt = (body_txt[:cap]
                        + "\n- …more rules truncated — call memory_lookup"
                          "(\"rules\") for the full list before acting")
        return ("RULES — MANDATORY, non-negotiable: the user ordered these "
                "ALWAYS followed, every session, HIGHEST priority (they "
                "override defaults and convenience). Check your plan against "
                "each rule before answering or acting:\n"
                + body_txt + ambiguous_note)
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


def _t_set_repo_folder(args: dict, cwd: str) -> dict:
    """Persist the local FOLDER for a repo so tickets/pipeline runs for that
    repo resolve to it — ``repo`` = the project name, ``path`` = its absolute
    local folder. Use when the user says 'use /x/y for repo foo' or 'repo foo
    lives at /x/y'. Stored in repos.json; read by the workspace resolver."""
    from aiforge_core.config import repo_map
    repo = str(args.get("repo") or "").strip()
    path = str(args.get("path") or "").strip()
    if not repo or not path:
        return {"ok": False, "error": "need repo and path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_path(repo, path)


def _t_set_repo_root(args: dict, cwd: str) -> dict:
    """Persist the GLOBAL base folder that holds all repos — ``path`` = the
    directory whose subfolders are repos (a ticket for project ``foo`` resolves
    to ``<path>/foo``). Use when the user says 'all repos live under /x' or
    'the global repo folder is /x'."""
    from aiforge_core.config import repo_map
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "need path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_default_root(path)


def _t_list_repos(args: dict, cwd: str) -> dict:
    """List the configured repo folders: the global base + explicit per-repo
    paths + the git repos found under the base."""
    from aiforge_core.config import repo_map
    import os as _os
    cfg = repo_map.list_all()
    root = cfg["default_root"]
    found = []
    try:
        for d in sorted(_os.listdir(root)):
            p = _os.path.join(root, d)
            if _os.path.isdir(_os.path.join(p, ".git")):
                found.append(d)
    except OSError:
        pass
    return {"ok": True, "default_root": root, "paths": cfg["paths"],
            "repos_under_root": found}


def _t_set_integration_default(args: dict, cwd: str) -> dict:
    """Persist a user-stated DEFAULT so later tool calls auto-fill it —
    ``tool`` = jira | confluence, ``value`` = the project key (jira) or space
    key (confluence). Deterministic: stored in the integrations config, read by
    jira_*/confluence_* on every call. Use when the user says e.g. 'use ENG as
    the default project' / 'default Confluence space is DEV'."""
    tool = str(args.get("tool") or "").strip().lower()
    value = str(args.get("value") or "").strip()
    if tool not in ("jira", "confluence"):
        return {"ok": False, "error": "tool must be 'jira' or 'confluence'"}
    if not value:
        return {"ok": False, "error": "missing 'value' (project/space key)"}
    field = "default_project" if tool == "jira" else "default_space"
    try:
        from aiforge_core.config import integrations
        integrations.set_(tool, {field: value})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "tool": tool, field: value,
            "note": f"{tool} calls will now default {field}={value} when omitted"}


def _t_jira_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_search(args, cwd)


def _t_jira_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_read(args, cwd)


def _t_jira_worklog(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_worklog(args, cwd)


def _t_jira_remote_links(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_remote_links(args, cwd)


def _t_resolve_repo(args: dict, cwd: str) -> dict:
    """Resolve a loosely-typed repo/service/folder name to its local path
    (tolerates case, spaces, missing hyphens, typos)."""
    from aiforge_core.config import repo_map
    return repo_map.resolve(args.get("name") or args.get("repo") or "")


def _t_jira_resolve_project(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_resolve_project(args, cwd)


def _t_confluence_resolve_space(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_resolve_space(args, cwd)


def _t_context_gather(args: dict, cwd: str) -> dict:
    """Assemble a cross-entity dossier (a Jira ticket + its linked Confluence
    pages + images, or vice versa) in PARALLEL, cache it in the context folder,
    and refresh only when the entity changed. Use when asked to explain/
    understand a ticket or page."""
    from aiforge_core.runtime import context_gather as _cg
    kind = (args.get("kind") or "").lower()
    key = str(args.get("key") or args.get("id") or "").strip()
    if not kind and key:
        # infer: a JIRA-KEY looks like PROJ-42 (case-insensitive); else a
        # numeric id → confluence. Normalize a jira key to uppercase.
        if re.match(r"^[A-Za-z][A-Za-z0-9]+-\d+$", key):
            kind, key = "jira", key.upper()
        else:
            kind = "confluence"
    if kind not in ("jira", "confluence") or not key:
        return {"ok": False, "error": "need kind (jira|confluence) + key/id"}
    return _cg.gather(kind, key, force=bool(args.get("force")),
                      role="chat")


def _t_jira_log_work(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_log_work(args, cwd)


def _t_jira_myself(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_myself(args, cwd)


def _t_jira_projects(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_projects(args, cwd)


def _t_jira_boards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_boards(args, cwd)


def _t_jira_sprints(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprints(args, cwd)


def _t_jira_sprint_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprint_issues(args, cwd)


def _t_jira_dashboards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboards(args, cwd)


def _t_jira_dashboard_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_read(args, cwd)


def _t_jira_dashboard_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_create(args, cwd)


def _t_jira_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_create(args, cwd)


def _t_jira_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_update(args, cwd)


def _t_jira_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_comment(args, cwd)


def _t_email_send(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_send(args, cwd)


def _t_email_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_read(args, cwd)


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


def _t_web_crawl(args: dict, cwd: str) -> dict:
    """Fetch a URL as clean markdown and file it as a work/web/<slug> dossier
    (crawl4ai when installed, tag-strip fetch fallback)."""
    from aiforge_core.runtime.tools import web_ingest
    return web_ingest.web_crawl(args, cwd)


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
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("skill", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _skills.write_skill(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
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
    so future sessions (or the user) can reuse it. scope: 'global' or 'repo'.
    Optional ``scripts`` land in the workflow's own ``scripts/`` folder;
    write_workflow HARD-tests each one (syntax check + actually RUNS its
    ``test`` command or the script itself) and REFUSES the save on any
    failure — job-builder parity, no honour-system flag."""
    try:
        from aiforge_core.runtime import workflows as _wf
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        scripts = args.get("scripts") or []
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("workflow", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _wf.write_workflow(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
            scripts=scripts,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ─────────────────────────── shared "strong" tools ──────────────────────────
# The OpenHands-parity tools (editor with undo + syntax-check, LSP, typecheck,
# format, test-runner) lived only in the ADK team pipeline. These thin adapters
# expose them to the deploy-anywhere chat agent too. They resolve through
# sandbox.root(); the dispatch loop scopes that override to the WORKSPACE root
# (set+reset in finally) for exactly these names — so a path can't escape an
# AIFORGE_WORKSPACE_DIR jail and a reused thread can't leak the dir.
# ipython (execute_ipython_cell) IS exposed to chat for Claude-Code/Cursor
# parity, but — because it runs arbitrary code in a kernel — it is
# approval-gated (in tool_policy._DEFAULT_ASK → ASK in Act mode, blocked in
# Plan mode) AND cwd-jailed here, so it can't run unapproved or escape the
# AIFORGE_WORKSPACE_DIR root the way the old unmanaged version did.
_ROOT_SCOPED_TOOLS = {"editor", "typecheck", "format", "lsp", "run_tests",
                      "execute_ipython_cell"}


def _scoped_root(cwd: str) -> str:
    """Root the strong tools should resolve against. Use the session ``cwd`` (so
    they hit the SAME files as file_read/file_write/multi_edit, and each parallel
    worktree stays isolated). Only when an AIFORGE_WORKSPACE_DIR jail is set AND
    cwd escapes it do we clamp to the jail root — so the strong tools can't write
    outside the jail, without collapsing every subtask onto one shared dir."""
    try:
        ws = _workspace_root()
        if ws is None:
            return cwd
        c = Path(cwd).expanduser().resolve()
        return str(c) if (c == ws or ws in c.parents) else str(ws)
    except Exception:  # noqa: BLE001
        return cwd


def _coerce_int(v, default=None):
    try:
        return int(v) if v is not None and str(v).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _t_editor(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.editor import editor
    vr = args.get("view_range")
    if isinstance(vr, list):
        vr = [_coerce_int(x) for x in vr]
        if any(x is None for x in vr):
            vr = None
    return editor(
        command=str(args.get("command") or args.get("sub_command") or "view"),
        path=str(args.get("path") or ""),
        file_text=args.get("file_text") if args.get("file_text") is not None else args.get("content"),
        old_str=args.get("old_str") if args.get("old_str") is not None else args.get("old_text"),
        new_str=args.get("new_str") if args.get("new_str") is not None else args.get("new_text"),
        insert_line=_coerce_int(args.get("insert_line")),
        view_range=vr,
    )


def _t_multi_edit(args: dict, cwd: str) -> dict:
    """Apply a BATCH of find/replace edits across one or more files in a single
    call — validated first, then applied atomically (snapshot + rollback). Each
    edit: ``{"path","old_str","new_str","replace_all"?}``."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list of "
                "{path, old_str, new_str, replace_all?}"}
    pending: dict[str, str] = {}        # abs_path -> working content (chained)
    original: dict[str, str] = {}       # abs_path -> pre-edit disk content (rollback)
    rel_of: dict[str, str] = {}         # abs_path -> the path the model gave
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return {"ok": False, "error": f"edit #{i} is not an object"}
        path = str(e.get("path") or "").strip()
        old = e.get("old_str") if e.get("old_str") is not None else e.get("old_text")
        new = e.get("new_str") if e.get("new_str") is not None else e.get("new_text")
        if not path or old is None or new is None:
            return {"ok": False, "error": f"edit #{i} needs path + old_str + new_str"}
        if old == "":
            return {"ok": False, "error": f"edit #{i}: old_str must be non-empty"}
        try:
            ap = str(_resolve(cwd, path))
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        rel_of.setdefault(ap, path)
        if ap not in pending:
            try:
                pending[ap] = Path(ap).read_text(encoding="utf-8", errors="replace")
                original[ap] = pending[ap]
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
    # Syntax-guard each resulting code file (skipped for non-code / force:true).
    for ap, content in pending.items():
        bad = _syntax_check(ap, content, args)
        if bad:
            return {"ok": False, "error": "syntax_invalid", "file": rel_of.get(ap, ap),
                    "detail": bad, "hint": "fix the edit or pass force:true"}
    # Phase 2 — write atomically: on ANY failure, roll every file back.
    written: list[str] = []
    done: list[str] = []
    try:
        for ap, content in pending.items():
            Path(ap).write_text(content, encoding="utf-8")
            done.append(ap)
            written.append(rel_of.get(ap, ap))
    except Exception as exc:  # noqa: BLE001 — restore the pre-edit state
        for ap in done:
            try:
                Path(ap).write_text(original[ap], encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "error": f"write failed, rolled back: {exc}"}
    return {"ok": True, "files": written, "edits_applied": len(edits)}


def _t_typecheck(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.typecheck import typecheck
    return typecheck()


def _t_format(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.format import format as _fmt
    return _fmt(str(args.get("path") or "."))


def _t_lsp(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.lsp import lsp
    return lsp(command=str(args.get("command") or ""), path=str(args.get("path") or ""),
               line=_coerce_int(args.get("line"), 0), character=_coerce_int(args.get("character"), 0))


def _t_run_tests(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.test_runner import run_tests
    return run_tests(mode=str(args.get("mode") or "fast"), pattern=str(args.get("pattern") or ""))


def _git_cli(argv: list, cwd: str, timeout: int = 30) -> dict:
    import subprocess
    try:
        r = subprocess.run(["git", *argv], cwd=cwd or ".", capture_output=True,
                           text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "code": r.returncode,
                "stdout": (r.stdout or "")[-8000:], "stderr": (r.stderr or "")[-2000:]}
    except FileNotFoundError:
        return {"ok": False, "error": "git_not_installed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_git_status(args: dict, cwd: str) -> dict:
    return _git_cli(["status", "--porcelain=v1", "-b"], cwd)


def _t_git_diff(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "diff"] + (["--staged"] if args.get("staged") else [])
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_git_log(args: dict, cwd: str) -> dict:
    n = max(1, min(_coerce_int(args.get("limit"), 20) or 20, 200))
    argv = ["--no-pager", "log", f"-{n}", "--oneline", "--decorate"]
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_jira_transitions(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transitions(args, cwd)


def _t_jira_transition(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transition(args, cwd)


def _t_jira_assign(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_assign(args, cwd)


def _t_jira_link_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_link_issues(args, cwd)


def _t_confluence_children(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_children(args, cwd)


def _t_confluence_attach(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_attach(args, cwd)


def _t_confluence_spaces(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_spaces(args, cwd)


def _t_confluence_page_by_title(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_page_by_title(args, cwd)


def _t_confluence_labels(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_labels(args, cwd)


def _t_confluence_add_label(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_add_label(args, cwd)


def _t_confluence_comments(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comments(args, cwd)


def _t_confluence_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comment(args, cwd)


def _t_confluence_descendants(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_descendants(args, cwd)


def _t_git_blame(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "blame", "--date=short"]
    _s, _e = _coerce_int(args.get("start")), _coerce_int(args.get("end"))
    if _s and _e:
        argv += ["-L", f"{_s},{_e}"]
    argv += ["--", str(args.get("path") or "")]
    return _git_cli(argv, cwd)


def _t_read_lines(args: dict, cwd: str) -> dict:
    import os as _os
    path = str(args.get("path") or "")
    fp = path if _os.path.isabs(path) else _os.path.join(cwd or ".", path)
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {path}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    n = len(lines)
    s = max(1, _coerce_int(args.get("start"), 1) or 1)
    e = n if not args.get("end") else min(_coerce_int(args.get("end"), n), n)
    if s > n:
        return {"ok": True, "path": path, "total_lines": n, "text": ""}
    return {"ok": True, "path": path, "start": s, "end": e, "total_lines": n,
            "text": "".join(lines[s - 1:e][:5000])[:60000]}


def _t_rename_symbol(args: dict, cwd: str) -> dict:
    import os as _os
    import re as _re
    name = str(args.get("name") or "")
    new = str(args.get("new_name") or "")
    if not name or not new:
        return {"ok": False, "error": "need 'name' and 'new_name'"}
    dry = args.get("dry_run", True)
    base = str(args.get("path") or ".")
    root_p = base if _os.path.isabs(base) else _os.path.join(cwd or ".", base)
    pat = _re.compile(r"\b" + _re.escape(name) + r"\b")
    _EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".c",
            ".cpp", ".h", ".cs", ".rb", ".php", ".kt", ".scala", ".swift")
    hits, changed = [], 0
    for dp, dn, fns in _os.walk(root_p):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv",
                 "venv", "dist", "build", "__pycache__")]
        for fn in fns:
            if not fn.endswith(_EXT):
                continue
            fpath = _os.path.join(dp, fn)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except Exception:  # noqa: BLE001
                continue
            c = len(pat.findall(txt))
            if not c:
                continue
            hits.append({"file": _os.path.relpath(fpath, cwd or "."),
                         "occurrences": c})
            if not dry:
                try:
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.write(pat.sub(new, txt))
                    changed += c
                except Exception:  # noqa: BLE001
                    pass
    return {"ok": True, "name": name, "new_name": new, "dry_run": bool(dry),
            "files": hits, "total_occurrences": sum(h["occurrences"] for h in hits),
            "applied": (0 if dry else changed)}


def _chat_run_id(cwd: str) -> str:
    """Stable per-workspace id so the browser tab / IPython kernel PERSIST
    across chat turns.

    Tool handlers only receive ``(args, cwd)`` — the chat ``session_id`` is not
    threaded down to them — so we derive a deterministic id from ``cwd``. Using
    a content hash (not the salted builtin ``hash``) keeps it stable across
    process restarts, so a reconnecting session reattaches to the same tab.
    """
    import hashlib
    digest = hashlib.md5((cwd or ".").encode("utf-8")).hexdigest()[:12]
    return f"chat-{digest}"


# --- Pipeline-parity tools: mcp, browser, jupyter, sub-agent delegate -------
# The team pipeline Doer has these four; the SIMPLE-CHAT agent now matches it
# (and Claude Code / Cursor, which expose browser + MCP + sub-agents in a single
# agent). All degrade soft: if the dep (playwright / jupyter_client) or import
# is unavailable, the handler returns {"ok": False, "error": ...} instead of
# raising into the chat loop.
def _t_mcp(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.mcp_client import mcp
        return mcp(str(args.get("command") or ""),
                   endpoint=args.get("endpoint"),
                   tool=args.get("tool"),
                   arguments=args.get("arguments"))
    except Exception as exc:  # noqa: BLE001 — soft-fail, never crash the chat
        return {"ok": False, "error": str(exc)}


def _t_browse(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.browser import browse
        return browse(str(args.get("command") or ""),
                      url=args.get("url"),
                      path=args.get("path"),
                      selector=args.get("selector"),
                      text=args.get("text"),
                      x=_coerce_int(args.get("x")),
                      y=_coerce_int(args.get("y")),
                      button=args.get("button"),
                      key=args.get("key"),
                      dx=_coerce_int(args.get("dx")),
                      dy=_coerce_int(args.get("dy")),
                      _run_id=_chat_run_id(cwd))
    except Exception as exc:  # noqa: BLE001 — playwright may be absent
        return {"ok": False, "error": str(exc)}


def _t_ipython(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.ipython_kernel import execute_ipython_cell
        kwargs: dict = {"_run_id": _chat_run_id(cwd)}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return execute_ipython_cell(str(args.get("code") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — jupyter_client may be absent
        return {"ok": False, "error": str(exc)}


def _t_delegate(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.delegation import delegate_to_agent
        kwargs: dict = {}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return delegate_to_agent(str(args.get("role") or ""),
                                 str(args.get("prompt") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — soft-fail
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
    "search_chat_sessions": _t_search_chat_sessions,
    "skill_search": _t_skill_search,
    "learn_skill": _t_learn_skill,
    "confluence_search": _t_confluence_search,
    "confluence_read": _t_confluence_read,
    "confluence_create": _t_confluence_create,
    "confluence_update": _t_confluence_update,
    "jira_search": _t_jira_search,
    "jira_read": _t_jira_read,
    "jira_worklog": _t_jira_worklog,
    "jira_log_work": _t_jira_log_work,
    "jira_remote_links": _t_jira_remote_links,
    "context_gather": _t_context_gather,
    "resolve_repo": _t_resolve_repo,
    "jira_resolve_project": _t_jira_resolve_project,
    "confluence_resolve_space": _t_confluence_resolve_space,
    "jira_myself": _t_jira_myself,
    "jira_projects": _t_jira_projects,
    "jira_boards": _t_jira_boards,
    "jira_sprints": _t_jira_sprints,
    "jira_sprint_issues": _t_jira_sprint_issues,
    "jira_dashboards": _t_jira_dashboards,
    "jira_dashboard_read": _t_jira_dashboard_read,
    "jira_dashboard_create": _t_jira_dashboard_create,
    "jira_create": _t_jira_create,
    "jira_update": _t_jira_update,
    "jira_comment": _t_jira_comment,
    "jira_transitions": _t_jira_transitions,
    "jira_transition": _t_jira_transition,
    "jira_assign": _t_jira_assign,
    "jira_link_issues": _t_jira_link_issues,
    "confluence_children": _t_confluence_children,
    "confluence_attach": _t_confluence_attach,
    "confluence_spaces": _t_confluence_spaces,
    "confluence_page_by_title": _t_confluence_page_by_title,
    "confluence_labels": _t_confluence_labels,
    "confluence_add_label": _t_confluence_add_label,
    "confluence_comments": _t_confluence_comments,
    "confluence_comment": _t_confluence_comment,
    "confluence_descendants": _t_confluence_descendants,
    "set_integration_default": _t_set_integration_default,
    "set_repo_folder": _t_set_repo_folder,
    "set_repo_root": _t_set_repo_root,
    "list_repos": _t_list_repos,
    "git_status": _t_git_status,
    "git_diff": _t_git_diff,
    "git_log": _t_git_log,
    "git_blame": _t_git_blame,
    "read_lines": _t_read_lines,
    "rename_symbol": _t_rename_symbol,
    "email_send": _t_email_send,
    "email_read": _t_email_read,
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
    "web_crawl": _t_web_crawl,
    "workflow_search": _t_workflow_search,
    "learn_workflow": _t_learn_workflow,
    "create_job_script": _t_create_job_script,
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
    # Pipeline-parity tools (mcp / browser / jupyter / sub-agent delegate).
    "mcp": _t_mcp,
    "browse": _t_browse,
    "execute_ipython_cell": _t_ipython,
    "delegate_to_agent": _t_delegate,
    "delegate": _t_delegate,   # alias
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
                   "search_chat_sessions", "graphify_lookup", "repo_map",
                   "skill_search", "confluence_search", "confluence_read",
                   "jira_search", "jira_read",
                   "email_read",
                   "gitlab_search", "gitlab_read",
                   "web_search", "web_fetch", "web_crawl", "workflow_search",
                   "lsp", "typecheck",   # code-intel: read-only, OK in plan mode
                   # git inspect + line-range read + jira/confluence reads +
                   # list_services — all read-only (also in tool_policy's
                   # _READONLY_ALWAYS_ALLOW); keep the two classifications in sync
                   # so Plan mode doesn't block a tool the policy calls read-only.
                   "git_status", "git_diff", "git_log", "git_blame",
                   "read_lines", "jira_transitions", "confluence_children",
                   "list_services",
                   # Jira/Confluence READ suite + resolvers + the cross-entity
                   # dossier — all read-only. These were ADDED after this list
                   # was written and drifted out of sync, so PLAN MODE blocked
                   # them ("can't read jira in plan mode"): the builtin
                   # jira-read/confluence-read skills route through
                   # context_gather, which this gate refused.
                   "context_gather", "resolve_repo", "jira_resolve_project",
                   "confluence_resolve_space", "jira_worklog", "jira_projects",
                   "jira_remote_links", "jira_boards", "jira_sprints",
                   "jira_sprint_issues", "jira_dashboards",
                   "jira_dashboard_read", "jira_myself",
                   "confluence_spaces", "confluence_page_by_title",
                   "confluence_labels", "confluence_comments",
                   "confluence_descendants")

# Builder-finalize tools — a successful call ENDS the interview (one per builder
# kind: job/skill/workflow/rule). Emitting `builder_done` lets the UI drop the
# session's sticky builder mode so follow-ups are normal chat.
_FINALIZE_TOOLS = frozenset({
    "create_job_script", "learn_skill", "learn_workflow", "remember_rule"})

# Builder finalize tool per kind + how many interview turns before we nudge the
# model to call it (a local model can otherwise chat forever without finalizing).
_BUILDER_FINALIZE_TOOL = {
    "job": "create_job_script", "skill": "learn_skill",
    "workflow": "learn_workflow", "rule": "remember_rule"}
try:
    _BUILDER_NUDGE_AFTER = max(2, int(os.environ.get("AIFORGE_BUILDER_NUDGE_AFTER", "6")))
except (TypeError, ValueError):
    _BUILDER_NUDGE_AFTER = 6

# File-mutating tools that the pre-apply "Review edits" gate (Gap D) holds for
# human Approve/Reject even when policy would auto-allow them.
_MUTATING = ("file_write", "file_create", "file_patch", "editor", "multi_edit",
             "format", "rename_symbol")

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


# The approval gate is where the operator accepts/rejects a write — they see the
# WHOLE thing (full page, full diff, full Jira body); it's just text the UI
# scrolls, so display content is UNCAPPED. The only bound is on diff COMPUTE:
# difflib is ~O(n·m), so past this size we show full new content instead of
# paying to compute a diff no one can read. Tunable.
try:
    _DIFF_COMPUTE_MAX = max(10_000, int(os.environ.get(
        "AIFORGE_APPROVAL_DIFF_COMPUTE_MAX", "60000")))
except (TypeError, ValueError):
    _DIFF_COMPUTE_MAX = 60_000


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
    a colored monospace block). ``_(no change)_`` when identical.

    The DIFF is uncapped (the operator reviews the whole change), but difflib is
    ~O(n·m): a huge↔huge rewrite could freeze the gate. When both sides exceed
    ``_DIFF_COMPUTE_MAX``, skip the diff and show the FULL new content instead —
    nothing is hidden, we just don't pay the quadratic cost to compute a diff no
    one can read anyway."""
    import difflib
    old, new = old or "", new or ""
    if len(old) > _DIFF_COMPUTE_MAX and len(new) > _DIFF_COMPUTE_MAX:
        return f"_(too large to diff — showing full new {label})_\n\n" + _fence(new)
    d = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"current {label}", tofile=f"new {label}", lineterm=""))
    return _fence(d, "diff") if d.strip() else "_(no change)_"


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
                return f"**Write `{path}`**\n\n" + _fence(diff, "diff")
            return f"**New file `{path}`** ({len(new)} bytes)\n\n" + _fence(
                str(new))
        if tool == "file_patch":
            return (f"**Patch `{args.get('path', '?')}`**\n\n" + _fence(
                f"- {str(args.get('old_text', ''))}\n"
                f"+ {str(args.get('new_text', ''))}", "diff"))
        if tool in ("run_command", "bash", "shell"):
            return "**Run command**\n\n" + _fence(str(args.get("cmd", "")), "bash")

        # ── integration writes → formatted markdown, not a JSON blob ──────
        if tool == "confluence_create":
            return (f"### Create Confluence page\n\n"
                    f"**Space:** `{args.get('space', '?')}` · "
                    f"**Title:** {args.get('title', '?')}\n\n"
                    f"**Body:**\n\n"
                    + _xhtml_to_md(str(args.get('body', ''))))
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
                        if cur_md else "**New body:**\n\n" + new_md)
            return out
        if tool == "jira_create":
            md = (f"### Create Jira issue\n\n"
                  f"**Project:** `{args.get('project', '?')}` · "
                  f"**Type:** {args.get('issuetype', 'Task')}"
                  + (f" · **Priority:** {args['priority']}" if args.get('priority') else "")
                  + f"\n\n**Summary:** {args.get('summary', '?')}\n")
            if args.get("description"):
                md += f"\n{str(args['description'])}\n"
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
                    f"{str(args.get('body', ''))}")
        if tool == "gitlab_create":
            md = (f"### Create GitLab issue\n\n"
                  f"**Project:** `{args.get('project', '?')}`\n\n"
                  f"**Title:** {args.get('title', '?')}\n")
            if args.get("description"):
                md += f"\n{str(args['description'])}\n"
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
                    f"{str(args.get('body', ''))}")
    except Exception:  # noqa: BLE001
        pass
    return _fence(json.dumps(args, default=str, indent=2), "json")

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
- file_write   {{"path": "...", "content": "..."}}      (creates/overwrites; code is syntax-checked before it lands — pass "force": true to override)
- file_patch   {{"path": "...", "old_text": "...", "new_text": "..."}}   (syntax-checked result; "force": true overrides)
- multi_edit   {{"edits": [{{"path":"a.py","old_str":"foo","new_str":"bar"}}, {{"path":"b.py","old_str":"x","new_str":"y","replace_all":true}}]}}
                (apply several find/replace edits across one or MANY files in ONE call — validated first, then all-or-nothing)
- list_dir     {{"path": "."}}
- find         {{"name": "controller", "kind": "dir"}}  (fuzzy-locate files/dirs by partial name)
- grep         {{"pattern": "TODO", "path": "src"}}      (recursive; tolerates a wrong path)
- run_command  {{"cmd": "ls -la", "timeout": 600}}
                (timeout is SECONDS, default 600. Don't pass a tiny value. For a
                TEST SUITE run ONE file or case first — e.g. `pytest tests/test_x.py::TestY`
                — not the whole suite; a full suite often exceeds any limit. A
                timeout returns PARTIAL output, not a failure — narrow or raise
                the timeout, never revert your edits over it.)
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
- remember_rule {{"text": "always use yarn", "description": "when to apply it", "triggers": ["yarn","install"], "scope": "repo"}}
                 (persist a user rule for every session; same frontmatter as skills/workflows — name/description/triggers/scope; scope global|repo)
- memory_lookup{{"query": "..."}}                        (recall from knowledge memory)
- search_chat_sessions {{"query": "...", "limit": 6}}     (find things you discussed with the user in PAST chat sessions)
- memory_write {{"text": "the durable fact", "kind": "note|gotcha|decision", "decision": false, "tags": ["tool:jira"], "scope": "global"}}
                (scope defaults to THIS ticket/page/repo; scope:"global" = a lesson recalled across ALL tickets/repos — use for general knowledge, keep ticket-specifics unscoped)
                (save a learning/decision for future recall; tag TOOL learnings "tool:jira|confluence|git|email|gitlab" so they resurface for that tool)
- skill_search {{"query": "..."}}                        (find reusable SKILL.md playbooks)
- learn_skill  {{"name": "...", "description": "when to use it", "body": "the step-by-step playbook", "triggers": ["word1","word2"], "scope": "global|repo"}}
                (author a reusable skill after solving something non-trivial — also recorded in memory)
- workflow_search {{"query": "..."}}                     (find reusable WORKFLOW.md end-to-end procedures)
- learn_workflow  {{"name": "...", "description": "when to use it", "body": "the end-to-end steps", "triggers": ["word1"], "scope": "global|repo", "scripts": [{{"name": "step1.sh", "content": "#!/usr/bin/env bash\\n...", "test": "bash step1.sh --dry-run"}}]}}
                (author a reusable multi-step workflow when the user asks or after running a repeatable procedure)
                (optional scripts land in the workflow's own scripts/ folder, chmod +x; the body should call them by path. HARD GATE: every script is syntax-checked AND its "test" command — default: the script itself, no args — is actually RUN; ANY failure refuses the whole save, so write scripts that terminate cleanly or give each a fast --dry-run test. "test": "skip" only for a genuinely prod-only script, justified in the body)
                (scripts needing Jira/Confluence/GitLab/email DATA must call `aiforge-tool <tool_name> '<json args>'` — the configured integration does the work; NEVER raw curl against the REST APIs)
- create_job_script {{"name": "...", "cron": "0 9 * * *", "script": "<bash script text>", "description": "optional"}}
                (JOB-BUILDER finalize: save the approved script to ~/.aiforge/jobs + schedule it as a recurring cron job — deterministic, no LLM per run)
- confluence_search {{"query": "..."}}  or  {{"cql": "space = ENG AND text ~ 'foo'"}}   (find pages)
- confluence_read   {{"id": "12345"}}  or  {{"title": "Page Title", "space": "ENG"}}      (read a page; body is storage XHTML)
- confluence_create {{"title": "...", "space": "ENG", "body": "<p>storage XHTML</p>", "parent_id": "123"}}   (new page — needs your Approve)
- confluence_update {{"id": "12345", "body": "<p>new storage XHTML</p>", "title": "optional"}}              (edit a page — needs your Approve)
- confluence_spaces {{}}                                                                  (list spaces)
- confluence_page_by_title {{"space": "ENG", "title": "Runbook"}}                          (find a page's id + version by exact title)
- confluence_children {{"id": "12345"}}  ·  confluence_descendants {{"id": "12345"}}       (direct child pages · ALL descendants deep)
- confluence_labels {{"id": "12345"}}  ·  confluence_add_label {{"id": "12345", "labels": ["runbook","ops"]}}   (read · add labels — add needs Approve)
- confluence_comments {{"id": "12345"}}  ·  confluence_comment {{"id": "12345", "body": "<p>note</p>"}}         (read · add a comment — add needs Approve)
- jira_search   {{"query": "..."}}  or  {{"jql": "project = ENG AND status = Open"}}   (find issues)
- jira_read     {{"key": "ENG-123"}}                                                    (read an issue: fields, comments + time tracking — original/remaining estimate, time spent)
- jira_search   {{"jql": "assignee = currentUser()", "time": true}}                     (add time:true to include estimate/spent per issue)
- jira_worklog  {{"key": "ENG-123"}}                                                    (all time LOGGED on an issue: who, how much, when + estimate/spent rollup — "how much time recorded on X")
- context_gather {{"kind": "jira", "key": "ENG-123"}}  or  {{"kind": "confluence", "key": "12345"}}   (BEST for "explain/understand ticket or page": pulls the entity + its linked Confluence pages / Jira tickets + images IN PARALLEL, caches in the ticket/page folder, refreshes only if changed — call this first, then read the returned dossier)
- jira_remote_links {{"key": "ENG-123"}}                                                (Confluence pages + web links attached to an issue)
- resolve_repo {{"name": "pos client backend"}}                                         (loosely-typed repo/service/folder → local path; tolerates case/spaces/missing-hyphens/typos — ALWAYS use before assuming a repo folder)
- jira_resolve_project {{"name": "one shell"}}                                          (loose project name → real Jira project key)
- confluence_resolve_space {{"name": "dev docs"}}                                       (loose space name → real Confluence space key)
- jira_log_work {{"key": "ENG-123", "time_spent": "2h 30m", "comment": "..."}}          (record time against an issue — needs your Approve)
- jira_myself   {{}}                                                                    (the current/authenticated user — resolve "me"/"my")
- jira_projects {{}}                                                                    (list projects the token can see)
- jira_boards   {{"project": "ENG"}}                                                    (list Agile boards)
- jira_sprints  {{"board_id": 42, "state": "active"}}                                   (list sprints on a board)
- jira_sprint_issues {{"sprint_id": 99, "time": true}}                                  (issues in a sprint, optionally with time)
- jira_dashboards {{}}                                                                  (list dashboards)
- jira_dashboard_read {{"id": 10000}}                                                   (read a dashboard + its gadgets)
- jira_dashboard_create {{"name": "Team Velocity", "description": "...", "share": "authenticated"}}   (create a dashboard — Cloud only; needs your Approve)
- jira_create   {{"project": "ENG", "summary": "...", "issuetype": "Task", "description": "..."}}   (new issue — needs your Approve)
- jira_update   {{"key": "ENG-123", "summary": "...", "description": "...", "labels": ["a","b"], "status": "In Progress"}}   (edit fields; `status` moves the workflow via a transition — needs your Approve)
- jira_transition {{"key": "ENG-123", "transition": "In Progress"}}                     (move status directly; `jira_transitions {{"key":"ENG-123"}}` lists what's available — needs your Approve)
- jira_comment  {{"key": "ENG-123", "body": "comment text"}}                            (add a comment — needs your Approve)
- set_integration_default {{"tool": "jira", "value": "ENG"}}  or  {{"tool": "confluence", "value": "DEV"}}   (persist a DEFAULT project/space — call this when the user says "use X as the default project/space"; later jira_*/confluence_* calls auto-fill it when omitted)
- set_repo_folder {{"repo": "foo", "path": "/abs/path/to/foo"}}   (persist the local folder for a repo — call when the user says "use /x/y for repo foo"; tickets for that repo then resolve to it)
- set_repo_root {{"path": "/abs/base"}}   (persist the GLOBAL base folder holding all repos — call when the user says "all repos live under /x"; project `foo` then resolves to `/x/foo`)
- list_repos {{}}   (show the configured base folder + per-repo paths + git repos found under the base)
- email_send    {{"to": "a@b.com", "subject": "...", "body": "..."}}   (send an email via the configured SMTP — optional "cc"/"bcc"/"html"; needs your Approve)
- email_read    {{"query": "...", "limit": 10}}                        (read recent inbox emails via IMAP — optional "folder"/"unseen_only")
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
INTEGRATION ACTIONS ARE TOOL CALLS, NOT FILES. When the user asks to create/update \
a JIRA ticket, a Confluence page, send an email, or open a PR, you MUST call the \
matching tool (jira_create / confluence_create / email_send / github_pr) — do NOT \
write a local .md/.txt file as a substitute and do NOT claim you "created a ticket" \
when you only wrote a file. If the tool returns `not_configured` (e.g. \
jira_not_configured), STOP and tell the user plainly that the integration isn't \
configured and what to set (the tool's `hint`), then offer a local draft as an \
explicit alternative — never silently switch the deliverable or invent that the \
user "clarified" or "changed their mind".
- web_search    {{"query": "rust tokio select! cancellation", "limit": 5}}   (search the open web — no key — when you're stuck / need current docs)
- web_fetch     {{"url": "https://...", "max_chars": 6000}}                  (read a result page's text)
- web_crawl     {{"url": "https://..."}}                                     (fetch a page as clean markdown AND save it to the shared work/web/<slug>/ dossier for reuse across sessions — prefer this over web_fetch when the page is documentation worth keeping)
- plan_progress {{"slug": "part-1", "status": "running|done|failed"}}        (multi-part request tracker: flip a checklist item so the user sees live progress — call when you start and finish each part)
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
- DRAW ON PRIOR CONTEXT: if the answer could depend on earlier discussions or \
an external system (rather than being answerable from what's in front of you), \
consult first — the RELEVANT PRIOR CHAT SESSIONS block above, search_chat_sessions \
for more of your past conversations, memory_lookup for durable facts, and the \
matching integration search (jira_search / confluence_search / gitlab_search). \
Only when it would actually help — don't search on every turn.
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
APPLICABLE SKILLS / APPLICABLE WORKFLOWS shown above are auto-selected for this \
request by relevance — when a task matches one, follow its steps AND reproduce \
any output format, structure, or naming convention it specifies EXACTLY, \
including every opening and closing delimiter. When it prescribes the exact \
output, produce it DIRECTLY — do not paraphrase, do not add commentary it \
forbids, and do not ask a clarifying question first.
- CAPTURE LEARNINGS (be your own learner + memory updater): when a session \
established something durable and reusable — a fix recipe, a gotcha+workaround, \
an architectural decision, a fact about how this repo works — persist it with \
memory_write before you FINAL, so future sessions recall it. Base it on the \
session summary / what you actually did and verified, not on trivia. Use \
kind="decision" (decision=true) for "we picked X over Y" choices, else \
kind="note"/"gotcha". Keep each fact one crisp sentence tied to a path/symbol; \
1-3 per session max, and do NOT re-save a fact already present in the recalled \
memory above (dedupe). Skip it entirely for trivial one-off answers. \
When the learning is about a TOOL — a working JQL/CQL, the right filter, a \
default project/space, an API quirk, a repo's build command — ADD a \
"tool:<name>" tag (tool:jira, tool:confluence, tool:git, tool:email, \
tool:gitlab) so it resurfaces next time you use that tool, instead of \
re-figuring it out (a recurring complaint when the same request repeats).
- MEMORY FIRST (for understanding/explaining code): before grepping the \
filesystem, call `memory_lookup(query)` — it semantically recalls the INDEXED \
codebase (tree-sitter symbols, code/doc chunks, the graphify concept graph) \
plus prior learnings and decisions from the knowledge memory. For any \
"explain / how does X work / where is Y / walk me through" question, \
memory_lookup FIRST (2-4 focused queries), use its hits to jump to the right \
files, THEN grep/read to confirm details. It is faster and broader than blind \
filesystem search. Only skip it if the memory returns nothing relevant, then \
fall back to grep/find. (If the answer needs a specific repo, its code lives \
under its real path — see APPLICABLE SKILLS — not this chat's scratch cwd.)
- USE THE CONTEXT ALREADY GATHERED FOR YOU: the RELEVANT MEMORY, REPO-MAP, \
APPLICABLE SKILLS/WORKFLOWS and PRIOR CHAT blocks above were auto-selected for \
THIS request — READ them before reaching for tools. Do not grep/list_dir to \
rediscover something the repo-map or memory block already tells you. Start \
from what's given, then use tools only to fill the specific gaps.
- LSP FOR PRECISION (not guessing): to find where a symbol is DEFINED, who \
CALLS it, or its type/signature, use `lsp` (goto_definition | find_references \
| hover) — it returns the exact location, unlike a text grep that matches \
comments and strings. Prefer lsp over grep for any "where is X defined / \
what calls X / what's the type of X" question; grep is for free-text/patterns.
- SCOPE before reading: when asked to check/review/understand code, first \
narrow to the FEW files that actually matter — use memory_lookup, then \
`grep`/`find` (and list_dir) to locate the relevant symbols/files, then read \
only those. Do NOT read every file in the repo; analysing irrelevant files \
wastes effort and context. Read broadly only when the task genuinely spans \
the codebase.
- When asked to RUN/BUILD/TEST a project: prefer the `project` tool — it \
auto-detects the stack (maven/gradle/node/react/next/vite/python/go/rust), \
installs the toolchain, and runs the right command. For anything it \
doesn't cover, fall back to run_command and do every step yourself \
(install deps → build → run). Execute, don't just describe.
- PROVE IT RUNS — don't make the user ask. After you write/change code (a \
POC, a feature, a bug fix), do NOT stop at "code written". After EVERY code \
change verify in this order and fix until each is green — never claim done on \
unverified code: (1) COMPILE — run the stack's compile/typecheck (mvn -q \
compile / go build ./... / tsc --noEmit / python -c 'import <mod>'); read the \
error, fix, re-run. (2) TEST — run the project's test command (pytest -x -q / \
npm test / mvn -q test), writing at least one test if none covers the change; \
on red, fix and re-run. (3) RUN — START the app with `serve` (it returns the \
pid + the URL) to confirm it boots, then stop_service(pid). (4) In your FINAL \
give the operator the exact COMPILE, TEST, and RUN commands + the endpoint/URL \
to open, so they can reproduce it. If unsure of the stack's commands, use the \
`project` tool (auto-detects maven/gradle/npm/vite/python/go/rust) or consult \
the stack-run-commands skill. If there are TWO services (e.g. an API + a web \
UI), `serve` BOTH and give both URLs and how they connect. Use `serve` for \
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


_REASONING_PREFIX_RE = re.compile(
    r"^[ \t]*(?:THOUGHT|THINK|THINKING|REASONING|ANALYSIS|PLAN|ACTION|FINAL)"
    r"[ \t]*:[ \t]*(?:FINAL\b[ \t]*)?",
    re.IGNORECASE)


def _strip_reasoning_prefix(text: str) -> str:
    """Strip a leaked chain-of-thought marker (``THOUGHT:``/``REASONING:`` …)
    from the START of a final answer. A local model sometimes emits its
    reasoning line as the answer (or a `FINAL:` whose text begins with
    `THOUGHT:`), so the user saw ``THOUGHT: The user asked me to…`` instead of
    the plan/answer. Only strips a LEADING marker; reasoning that legitimately
    appears mid-answer is untouched."""
    if not text:
        return text
    t = text.lstrip()
    m = _REASONING_PREFIX_RE.match(t)
    if not m:
        return text
    rest = t[m.end():]
    # Drop only the first reasoning line; keep everything after it. If the whole
    # thing was one reasoning line with nothing useful after, keep it (better a
    # thought than an empty answer).
    nl = rest.find("\n")
    tail = rest[nl + 1:].lstrip() if nl != -1 else ""
    return tail or rest.strip() or text


def _parse(out: str) -> dict:
    """Parse a model turn into {kind, ...}. Tolerant of code fences,
    pretty-printed JSON, and stray markdown around the protocol."""
    fin = _FINAL_RE.search(out)
    ask = _ASK_RE.search(out)
    act = _ACTION_RE.search(out)
    # Prefer ACTION when present (models sometimes mention "final" in prose).
    if act:
        name = act.group(1).strip()
        # Some models emit the completion as a fake TOOL call —
        # `ACTION: final ARGS_JSON: {"text": "…"}` — instead of the `FINAL:`
        # marker. Dispatching that hits "unknown tool: final" and the model
        # loops. Coerce a completion pseudo-tool into a real final answer.
        if name.lower() in ("final", "finish", "done", "complete", "final_answer"):
            m2 = re.search(r"ARGS_JSON\s*:?", out, re.IGNORECASE)
            fargs = _balanced_json(out, m2.end() if m2 else act.end())
            txt = ""
            if isinstance(fargs, dict):
                txt = str(fargs.get("text") or fargs.get("answer")
                          or fargs.get("response") or fargs.get("content") or "")
            # No JSON args — the answer is the plain text the model wrote AFTER
            # the `ACTION: FINAL` marker (its reasoning sits ABOVE the marker).
            # Fall back to that slice, NOT the whole turn, or the thought +
            # marker leak into the answer and break a skill's "nothing else"
            # format. Only use the whole turn as a last resort (marker at EOF).
            if not txt.strip():
                after = out[act.end():].strip()
                txt = after or out.strip()
            return {"kind": "final", "text": txt.strip()}
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
    # No FINAL/ASK/ACTION marker. If the model was mid-reasoning — it emitted a
    # THOUGHT (intent to act) but no ACTION — it almost certainly got truncated
    # or forgot to emit the ACTION line. Treating that as the final answer stops
    # the run early ("Now I need to create the script… Let me first check…" then
    # nothing). Signal CONTINUE so the loop nudges it to act instead of ending.
    tho = _THOUGHT_RE.search(out)
    if tho:
        return {"kind": "continue", "thought": tho.group(1).strip() or out.strip()}
    # Genuinely just prose with no protocol at all → treat as the final answer.
    # Tag it IMPLICIT (no explicit ``FINAL:`` marker): in interactive chat that's
    # the real answer, but in a work-producing run (doer / builder) it's usually
    # premature narration ("let me test what's happening…") and the loop should
    # nudge-and-continue rather than quit — see the ``final`` branch in the loop.
    return {"kind": "final", "text": out.strip(), "implicit": True}


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


# Below this resolved context window (tokens) a small local box gets lean
# ("cave") context automatically — the operator needn't flip a setting.
# Override the threshold with AIFORGE_CAVE_AUTO_WINDOW; a big-window model
# stays above it and keeps the full context.
_CAVE_AUTO_WINDOW_DEFAULT = 49152   # 48K


def _cave_auto_window() -> int:
    raw = os.environ.get("AIFORGE_CAVE_AUTO_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _CAVE_AUTO_WINDOW_DEFAULT


def _resolved_window(role: str | None = None) -> int:
    """The resolved context window in tokens. Routes through the ONE window
    source (``model_registry.effective_context_window``) so cave sizing, the
    seed/sys-prompt budgets and the window-scaled caps all agree (A3): prefer
    the per-role registry / auto-detected window, else the global setting."""
    try:
        from aiforge_core.config import model_registry
        return int(model_registry.effective_context_window(role))
    except Exception:  # noqa: BLE001
        try:
            from aiforge_core.config import runtime_settings
            return int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            return 131072


def _window_scaled(floor: int, frac: float, role: str | None = None) -> int:
    """A window-relative section cap: ``max(floor, window_chars × frac)``.

    The floor is today's fixed value (so a 32K window is byte-identical); on a
    bigger window the cap grows with it so a 256K box is actually used. Uses the
    SAME resolved-window source as every other budget (A3). Soft-fails to the
    floor on any error."""
    try:
        win = _resolved_window(role)
        return max(floor, int(win * 4 * frac))
    except Exception:  # noqa: BLE001
        return floor


def _cave_mode() -> bool:
    """Cave mode = leanest useful context (smaller repo map, skip optional
    skills/workflows/mentions blocks, fewer memory hits, tighter condense
    budget).

    Resolution — an EXPLICIT operator choice always wins, in order:
      1. env ``AIFORGE_CAVE_MODE`` (1/0 force on/off)
      2. an explicitly-stored ``cave_mode`` setting (UI wrote it — 1/0)
    Only when NEITHER is set do we AUTO-enable cave for a small resolved
    context window (<= ``AIFORGE_CAVE_AUTO_WINDOW``, default 48K), so a
    small local box gets lean context without the operator flipping a
    setting while a big-window model keeps the full context."""
    env = os.environ.get("AIFORGE_CAVE_MODE")
    if env is not None:
        return env not in ("0", "false", "")
    try:
        from aiforge_core.config import runtime_settings
        # Distinguish an explicitly-stored value from the unset default (a
        # stored 0 = operator opted OUT and must be respected).
        stored = runtime_settings._read_store().get("cave_mode")
        if isinstance(stored, int):
            return stored > 0
    except Exception:  # noqa: BLE001
        pass
    # Unset → auto-enable when the window is small enough that the full
    # context wouldn't fit comfortably.
    try:
        return _resolved_window() <= _cave_auto_window()
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


# Measured size of the built system prompt (``_SYSTEM``) in chars — reserved
# out of the window so it isn't counted as available history.
_SYSTEM_PROMPT_CHARS = 14000
# Never let the history budget collapse to <=0 on a tiny window.
_CTX_BUDGET_FLOOR_CHARS = 4000


def _ctx_budget_chars(role: str | None = None,
                      sys_chars: int | None = None) -> int:
    """Char budget for the running conversation before auto-condensing. 0
    disables. Explicit override: AIFORGE_CHAT_CONTEXT_BUDGET_CHARS. Otherwise
    SIZED TO THE CONFIGURED MODEL WINDOW (context_window tokens → ~4 chars/token)
    MINUS the reservations that aren't available for history — the output cap
    (``max_output_tokens``) and the system prompt — so on a 32K local window the
    budget leaves real room for INPUT instead of assuming the whole window is
    history. ``sys_chars`` reserves the ACTUAL assembled system-prompt size when
    the caller knows it (M1); when omitted it falls back to the ~14K
    ``_SYSTEM_PROMPT_CHARS`` estimate. A cave/non-cave headroom fraction is then
    applied to the remaining usable space, and a floor keeps the budget positive
    on a tiny window."""
    env = os.environ.get("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    reserve_sys = _SYSTEM_PROMPT_CHARS if sys_chars is None else max(0, int(sys_chars))
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
    # Fraction of the (post-reserve) window kept as live history before we
    # condense. This is a SAFETY margin ON TOP of the explicit output + system
    # reservations below (the 4-chars/token estimate is imprecise and a turn's
    # tool output can still grow mid-flight), so it is < 1.0 on purpose — but
    # the old 0.55 was too conservative and made a big window (e.g. a 256k model)
    # feel like ~120k. 0.85 uses most of a large window (256k → condense ~210k)
    # while still leaving a ~15% cushion for the 4-chars/token estimate error +
    # mid-turn tool growth. Cave mode still condenses sooner. Tunable per deploy.
    _default_frac = 0.40 if _cave_mode() else 0.85
    try:
        headroom = float(os.environ.get("AIFORGE_CTX_HISTORY_FRACTION", _default_frac))
        headroom = min(0.95, max(0.15, headroom))  # clamp to a sane band
    except (TypeError, ValueError):
        headroom = _default_frac
    if win > 0:
        # Reserve what the request needs beyond history: the model's own reply
        # (output cap) and the system prompt. ~4 chars/token.
        try:
            from aiforge_core.config import runtime_settings
            out_chars = int(runtime_settings.get("max_output_tokens")) * 4
        except Exception:  # noqa: BLE001
            out_chars = 4096 * 4
        usable = win * 4 - out_chars - reserve_sys
        budget = int(max(usable, _CTX_BUDGET_FLOOR_CHARS) * headroom)
        return max(budget, _CTX_BUDGET_FLOOR_CHARS)
    return 24000 if _cave_mode() else 48000


# ── system-prompt budgeting (Fix C2) ────────────────────────────────────
# convo[0] (the system message) is NEVER shrunk by _compact_convo (it only
# condenses convo[1:-keep_recent]). Every dynamic block (repo summary/map,
# skills, workflows, mentions, memory + chat recall, images) is appended to
# it, so on a small window the un-condensable system prompt alone can
# overflow. We cap the assembled system prompt to a fraction of the window,
# dropping/truncating the LOWEST-priority injected blocks first and always
# keeping the core prompt + rules.
_SYS_PROMPT_FLOOR_CHARS = 8000


def _sys_prompt_frac() -> float:
    """Fraction of the window reserved for the (un-condensable) system prompt.
    Default 0.35 (env ``AIFORGE_SYS_PROMPT_FRAC``) — co-budgeted with the Doer
    seed (also 0.35) + the output cap so seed+sysprompt+output ≤ window (A1)."""
    try:
        return float(os.environ.get("AIFORGE_SYS_PROMPT_FRAC", "0.35"))
    except (TypeError, ValueError):
        return 0.35


def _sys_prompt_budget_chars(role: str | None = None) -> int:
    """Char cap for the assembled system prompt = :func:`_sys_prompt_frac` of
    the resolved window in chars, floored (scaled down on small windows).
    Threads ``role`` so it uses the SAME per-role window as the seed + history
    budgets (A3).

    C1: co-budgeted with the Doer seed + the output reservation so
    ``seed + sysprompt + out ≤ window×4`` holds even at 4K/8K/16K, where the
    fixed 8000 floor + a full-window output reservation used to overflow. The
    output reservation is capped at a window fraction and the floor scales
    down with whatever is left."""
    try:
        win = _resolved_window(role)
    except Exception:  # noqa: BLE001
        win = 32768
    win_chars = win * 4
    sys_frac = _sys_prompt_frac()
    try:
        from aiforge_core.runtime import text_doer as _td
        from aiforge_core.config import runtime_settings
        out_tok_chars = int(runtime_settings.get("max_output_tokens")) * 4
        out_chars = _td._out_reserve_chars(win_chars, out_tok_chars)
    except Exception:  # noqa: BLE001
        out_chars = min(8192 * 4, int(win_chars * 0.4))
    sys_reserve = int(win_chars * sys_frac)
    usable = win_chars - out_chars - sys_reserve
    floor = min(_SYS_PROMPT_FLOOR_CHARS, max(0, usable) // 3)
    return max(sys_reserve, floor)


_SYS_CAP_MARK = "\n…(system prompt truncated to fit context window)\n"


def _cap_system_prompt(sys_msg: str, budget: int, *, protect: int = 0) -> str:
    """Guarantee ``len(sys_msg) <= budget`` — the backstop under the block-aware
    assembly. Preserves the first ``protect`` chars (the core prompt + rules)
    and truncates the lower-priority injected TAIL first; if even the core
    exceeds the budget it hard-truncates. No-op when already under cap or
    ``budget <= 0``. Soft: never raises."""
    try:
        if budget <= 0 or len(sys_msg) <= budget:
            return sys_msg
        # Keep the first `budget - marker` chars (the core prompt + rules sit at
        # the FRONT, so the front-preserving cut drops the injected tail first).
        keep = budget - len(_SYS_CAP_MARK)
        if keep <= 0:
            return sys_msg[:max(0, budget)]
        return sys_msg[:keep] + _SYS_CAP_MARK
    except Exception:  # noqa: BLE001
        return sys_msg


def _compact_mode() -> str:
    """'llm' = summarise the dropped middle with the model (code-aware);
    'heuristic' (default) = cheap rolling breadcrumb, no extra LLM call."""
    m = os.environ.get("AIFORGE_COMPACT_MODE", "").strip().lower()
    if m in ("llm", "heuristic"):
        return m
    try:
        from aiforge_core.config import runtime_settings
        return "llm" if int(runtime_settings.get("compact_llm")) > 0 else "heuristic"
    except Exception:  # noqa: BLE001
        return "heuristic"


_COMPACT_SYS = (
    "You compress an earlier slice of a coding-assistant conversation into a "
    "DENSE, CODE-AWARE summary the assistant can rely on after the raw turns are "
    "dropped. Preserve, concretely: files/paths touched, function/class/symbol "
    "names, decisions made + their rationale, errors hit + fixes, commands run + "
    "outcomes, and any unresolved threads or the user's standing asks. Drop "
    "pleasantries and dead ends. Output 4-12 terse bullet lines, no preamble.")


def _text_of(m: dict) -> str:
    """Text of a chat message — handles the multimodal LIST form (a vision turn
    rewrites content to ``[{type:text,...}, {image...}]``) so callers never call
    .strip() on a list (which crashed the compactor)."""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) else ""


def _condense_timeout_s() -> float:
    """Wall-clock cap for the condense summariser call (env
    ``AIFORGE_CONDENSE_TIMEOUT_S``, default 30s; <=0 disables). On the Doer
    path ``session_id is None`` so ``_complete_cancellable`` runs the LLM call
    INLINE with no timeout — a wedged endpoint would hang the whole turn on a
    condense. This bounds it so the turn falls back to the non-LLM breadcrumb."""
    try:
        return float(os.environ.get("AIFORGE_CONDENSE_TIMEOUT_S", "30"))
    except (TypeError, ValueError):
        return 30.0


def _llm_summarize_middle(middle: list[dict], complete_fn, session_id=None) -> str:
    """Code-aware LLM summary of the dropped middle. Swappable model via
    AIFORGE_COMPACT_ROLE. Routed through _complete_cancellable so a Stop can
    interrupt it (and it honours the generation cap). Bounded by a wall-clock
    timeout (:func:`_condense_timeout_s`) so a hung endpoint can't wedge the
    turn. Returns '' on any failure / cancel / timeout so the caller falls
    back to the heuristic breadcrumb."""
    if complete_fn is None or not middle:
        return ""
    transcript = []
    for m in middle:
        r = (m.get("role") or "").upper()
        c = _text_of(m).strip()
        if c:
            transcript.append(f"{r}: {c}")
    body = "\n".join(transcript)
    if len(body) > 24000:        # bound the summariser's own input
        body = body[:12000] + "\n…\n" + body[-12000:]
    sum_role = os.environ.get("AIFORGE_COMPACT_ROLE", "").strip() or "doer"
    msgs = [{"role": "system", "content": _COMPACT_SYS},
            {"role": "user", "content": "Summarise this slice:\n\n" + body}]
    timeout = _condense_timeout_s()

    def _call() -> str:
        try:
            out = _complete_cancellable(complete_fn, sum_role, msgs, session_id)
            if out is _CANCELLED or not isinstance(out, str):
                return ""
            return out.strip()
        except Exception:  # noqa: BLE001
            return ""

    if timeout <= 0:
        return _call()
    import threading as _th
    box: dict = {}

    def _worker() -> None:
        box["out"] = _call()

    t = _th.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # Summariser is wedged — abandon it (daemon) and fall back to the
        # cheap non-LLM condense so the turn proceeds.
        return ""
    return box.get("out", "")


def _compact_convo(convo: list[dict], *, keep_recent: int = 18, role: str | None = None,
                   complete_fn=None, session_id=None) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — no extra LLM call, so
    it's cheap and runs every turn. The agent can re-read files / ask the user
    if it needs detail from before the condense point."""
    # M1: reserve the ACTUAL system-prompt size (convo[0]) rather than the fixed
    # 14K estimate, and DON'T re-count it in the over-budget sum below (it's
    # reserved, not history) — the old code both subtracted a constant AND
    # summed the real system chars = a double-count.
    sys_chars = (len(_text_of(convo[0]))
                 if convo and convo[0].get("role") == "system" else 0)
    budget = _ctx_budget_chars(role, sys_chars=sys_chars)
    if budget <= 0:
        return convo
    # Scale the verbatim tail to the budget: on a SMALL window, keeping 18 turns
    # could itself exceed the budget (condense fires but can't get under it).
    # ~2k chars/turn heuristic, floor 4 so there's always a usable recent slice.
    keep_recent = max(4, min(keep_recent, budget // 2000))
    if len(convo) <= keep_recent + 2:
        return convo
    if sum(len(_text_of(m)) for m in convo[1:]) <= budget:
        return convo
    tail = convo[-keep_recent:]
    middle = convo[1:-keep_recent]
    if not middle:
        return convo
    tools: list[str] = []
    user_asks: list[str] = []
    finals: list[str] = []
    for m in middle:
        mrole = m.get("role")
        content = _text_of(m).strip()
        if mrole == "assistant":
            mt = _ACTION_RE.search(content)
            if mt:
                tools.append(mt.group(1))
            # An assistant FINAL (no ACTION:) is a substantive outcome — keep a
            # short trace so the summary carries decisions, not just tool counts.
            elif content and "ACTION:" not in content:
                finals.append(content.replace("\n", " ")[:160])
        elif mrole == "user" and content and not content.startswith("OBSERVATION:"):
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
    # Optional CODE-AWARE LLM summary of the dropped middle (swappable model via
    # AIFORGE_COMPACT_ROLE). Falls back to the heuristic breadcrumb on failure.
    llm_summary = ""
    if _compact_mode() == "llm":
        llm_summary = _llm_summarize_middle(middle, complete_fn, session_id)
    # Wrap the breadcrumb in a unique sentinel so the next condense can strip
    # exactly THIS block (not a look-alike phrase a rule/skill might contain).
    if llm_summary:
        # Append the structured asks/outcomes tail so the NEXT condense's parser
        # can still carry the thread forward (without it, repeated condenses in
        # LLM mode silently dropped everything before the prior summary).
        note = (f"{_CONDENSE_OPEN}\n"
                f"[earlier conversation auto-condensed — {len(middle)} messages "
                f"omitted. Summary of what happened:\n{llm_summary}\n{summary}\n"
                "Re-read a file or ask the user if you need more detail.]\n"
                f"{_CONDENSE_CLOSE}")
    else:
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


def _ctx_on(block: str) -> bool:
    """Is the dynamic-context ``block`` injected this turn? Operator knob —
    ``ctx_no_{block}`` (runtime setting / env) = 1 turns it off. Default ON.
    Blocks: recall · mentions · skills · workflows · repomap · summary."""
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get(f"ctx_no_{block}")) == 0
    except Exception:  # noqa: BLE001
        return True


def _repomap_max_chars() -> int:
    """Char cap for the repo-map block. An explicit ``AIFORGE_REPOMAP_MAX_CHARS``
    wins verbatim (0 disables); otherwise window-relative (A2): floor 6000,
    growing at ~2% of the window."""
    env = os.environ.get("AIFORGE_REPOMAP_MAX_CHARS")
    if env is not None:
        try:
            return max(0, int(env))
        except (TypeError, ValueError):
            pass
    return _window_scaled(6000, 0.02)


_SYM_PATTERNS = {
    ".py": r"^\s*(?:async\s+)?(?:class|def)\s+(\w+)",
    ".java": r"^\s*(?:@\w+\s*)*(?:public|private|protected|static|final|abstract|\s)*"
             r"(?:class|interface|enum|record)\s+(\w+)"
             r"|^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s.]+?\s+(\w+)\s*\(",
    ".go": r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)|^\s*type\s+(\w+)\s",
    ".ts": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const|enum)\s+(\w+)",
    ".tsx": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const)\s+(\w+)",
    ".js": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+(\w+)",
    ".rb": r"^\s*(?:class|module|def)\s+([\w.]+)",
    ".rs": r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)",
    ".c": r"^\s*[\w\*\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cpp": r"^\s*(?:class|struct)\s+(\w+)|^\s*[\w:<>\*&\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cs": r"^\s*(?:public|private|protected|internal|static|\s)*(?:class|interface|struct|enum)\s+(\w+)",
    ".kt": r"^\s*(?:fun|class|interface|object)\s+(\w+)",
    ".php": r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|function)\s+(\w+)",
}


def _build_symbol_map(cwd: str, max_files: int = 200, max_syms: int = 12) -> str:
    """A lightweight, dependency-free repo map: each source file → its top-level
    symbols (classes/functions/methods) via regex. Fast (no tree-sitter/aider),
    language-agnostic, so the agent navigates by SYMBOLS not blind `find`."""
    import re as _re
    base = str(_workspace_root() or cwd)
    compiled = {ext: _re.compile(pat, _re.MULTILINE)
                for ext, pat in _SYM_PATTERNS.items()}
    rows: list[tuple[str, list[str]]] = []
    seen = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext not in compiled:
                continue
            if seen >= max_files:
                return _fmt_symbol_rows(base, rows, truncated=True)
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    src = fh.read(200_000)
            except Exception:  # noqa: BLE001
                continue
            syms: list[str] = []
            for m in compiled[ext].finditer(src):
                nm = next((g for g in m.groups() if g), None)
                if nm and nm not in syms and nm not in ("if", "for", "while",
                                                        "switch", "catch", "return"):
                    syms.append(nm)
                if len(syms) >= max_syms:
                    break
            if syms:
                rows.append((os.path.relpath(fp, base), syms))
                seen += 1
    return _fmt_symbol_rows(base, rows, truncated=False)


def _fmt_symbol_rows(base: str, rows: list, truncated: bool) -> str:
    if not rows:
        return ""
    cap = _repomap_max_chars()
    out: list[str] = []
    total = 0
    for rel, syms in rows:
        line = f"{rel}: {', '.join(syms)}"
        if cap and total + len(line) > cap:
            truncated = True
            break
        out.append(line)
        total += len(line) + 1
    body = "\n".join(out)
    tail = "\n… (more — grep/find for the rest)" if truncated else ""
    return body + tail


def _build_repo_map(cwd: str, max_entries: int = 160, max_depth: int = 3) -> str:
    """Repo map for the system prompt so the agent navigates by SYMBOLS, not blind
    `find`. Prefers the tree-sitter + PageRank Aider RepoMap (ranked functions/
    classes per file) — critical on big repos where a bare file tree is useless;
    falls back to a compact directory tree. Best-effort, char-capped."""
    base = str(_workspace_root() or cwd)
    if not os.path.isdir(base):
        return f"WORKING DIRECTORY: {base} (not a directory)"
    # 1. Tree-sitter Aider RepoMap — ranked symbols (the good map for analysis).
    #    TIME-BOUNDED: the first parse of a big repo can be slow, so run it in a
    #    thread with a short budget (AIFORGE_REPOMAP_BUDGET_S, default 6s). If it
    #    doesn't finish in time, fall through to the instant dir tree — the cached
    #    Aider map then serves later turns. Never blocks the turn.
    if os.environ.get("AIFORGE_CHAT_AIDER_MAP", "1") not in ("0", "false"):
        try:
            budget = float(os.environ.get("AIFORGE_REPOMAP_BUDGET_S", "30"))
        except ValueError:
            budget = 6.0
        _out: dict = {}

        def _work():
            try:
                from aiforge_core.memory.code_context import aider_digest
                _out["d"] = aider_digest(base, [])
            except Exception:  # noqa: BLE001
                _out["d"] = ""
        import threading as _th
        _t = _th.Thread(target=_work, daemon=True)
        _t.start()
        _t.join(budget)
        digest = _out.get("d") or ""
        if digest.strip():
            cap = _repomap_max_chars()
            if cap and len(digest) > cap:
                digest = digest[:cap] + "\n… (truncated — grep/find/list_dir for more)"
            return ("REPO MAP (ranked symbols via tree-sitter — the key functions/"
                    "classes per file; navigate by these, don't blind-`find`):\n"
                    f"WORKING DIRECTORY: {base}\n{digest}")
        # else: aider absent / timed out / empty → lightweight regex symbol map.

    # 2. Lightweight regex symbol map (no deps, fast) — file → its symbols.
    if os.environ.get("AIFORGE_CHAT_SYMBOL_MAP", "1") not in ("0", "false"):
        try:
            symmap = _build_symbol_map(base)
            if symmap and symmap.strip():
                return ("REPO MAP (each file → its top-level classes/functions; "
                        "navigate by these symbols, don't blind-`find`):\n"
                        f"WORKING DIRECTORY: {base}\n{symmap}")
        except Exception:  # noqa: BLE001
            pass
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
    # Char cap — the line/depth caps above bound entries but a wide tree can
    # still be huge. Window-relative (A2): floor 6000 on a 32K window, grows
    # with a bigger window so a 256K box shows a fuller map.
    cap = _repomap_max_chars()
    if cap and len(tree) > cap:
        tree = tree[:cap] + "\n… (truncated to fit context — use find/grep/list_dir)"
    return ("REPO MAP of the working directory (already known — do NOT "
            f"re-list directories you can see here):\nWORKING DIRECTORY: {base}\n"
            f"{tree}")


def _repo_name(cwd: str) -> str:
    # Canonical resolver (git-toplevel) — was workspace-dir basename, which
    # drifted from the recall key. Delegates now.
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")


# Keyword → tool-scope tag: which tool a request is likely to use. A recalled
# learning tagged ``tool:<name>`` (see the learner guidance) gets a score bump
# in recall so the working JQL/filter/config the agent figured out LAST time
# resurfaces for the same TYPE of request — instead of re-deriving it.
_TOOL_TAG_HINTS = {
    "tool:jira": ("jira", "jql", "issue", "ticket", "sprint", "epic"),
    "tool:confluence": ("confluence", "wiki", "space", "page"),
    "tool:git": ("git", "branch", "commit", "rebase", "pull request", " pr ", "merge"),
    "tool:email": ("email", "smtp", "inbox", "mailbox"),
    "tool:gitlab": ("gitlab", "merge request", " mr "),
}


def _tool_tags(query: str) -> list[str]:
    q = f" {(query or '').lower()} "
    return [tag for tag, kws in _TOOL_TAG_HINTS.items()
            if any(k in q for k in kws)]


_ASK_LEAD_RE = re.compile(
    r"^(?:also|and|plus|then|next|additionally|why|how|what|when|where|which|"
    r"who|can|could|should|would|is|are|does|do|did|will|fix|add|make|check|"
    r"recheck|verify|update|create|remove|delete|use|show|explain|list|"
    r"implement|write|run|test|deploy|review|rename|refactor|change|ensure)\b",
    re.IGNORECASE)


def _split_asks(text: str, cap: int = 8) -> list[str]:
    """Break the user's CURRENT message into its distinct asks so a
    multi-part message ("fix X. also why does Y happen? and add Z") gets a
    CHECKLIST instead of the model answering part 1 and stopping — simple
    mode has no enhancer/spec, so nothing else tracks the parts. Heuristic
    and conservative: bullets/numbered lines count as-is; otherwise sentence
    segments that look like a question or an imperative. Returns [] (no
    checklist) when only one ask is found."""
    t = (text or "").strip()
    if len(t) < 25:
        return []
    parts: list[str] = []
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullets = [re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", ln) for ln in lines
               if re.match(r"^(?:[-*•]|\d+[.)])\s+", ln)]
    if len(bullets) >= 2:
        parts = bullets
    else:
        # sentence segmentation + " also "/" and then " connectors
        segs: list[str] = []
        for chunk in re.split(r"(?<=[?.!;])\s+|\n+", t):
            segs.extend(re.split(
                r"\s+(?=(?:also|and then|and also|plus|additionally)\b)",
                chunk, flags=re.IGNORECASE))
        for s in segs:
            s = s.strip(" .")
            if len(s) < 12:
                continue
            if s.endswith("?") or _ASK_LEAD_RE.match(s):
                parts.append(s)
    parts = [p[:160] for p in parts if p.strip()][:cap]
    return parts if len(parts) >= 2 else []


def _memory_recall(cwd: str, query: str, limit: int = 6,
                   session_id: "int | None" = None) -> str:
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
        # F2/M3: recall under the SAME repo the chat WRITE path files facts
        # under (git-toplevel basename), else sqlite_memory.recall filters
        # them out (WHERE repo=?). M4: exclude the current live session so
        # this turn's own messages don't return as "prior chat".
        _repo = _chat_repo_key(cwd)
        res = _uq.query(q, limit=limit, repo=_repo,
                        exclude_session=session_id,
                        boost_tags=_tool_tags(q))
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


def _chat_session_recall(query: str, session_id: "int | None",
                         limit: int = 4) -> str:
    """Proactive recall from PRIOR CHAT SESSIONS — surface things the user
    discussed in OTHER conversations that may bear on this request, so simple
    chat has continuity across sessions (not just within one). Cheap + local
    (one SQLite scan). Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    try:
        from aiforge_core.runtime import chat_store
        hits = chat_store.search_messages(q, limit=limit,
                                          exclude_session=session_id)
    except Exception:  # noqa: BLE001
        hits = []
    lines: list[str] = []
    for h in hits:
        content = (h.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        title = h.get("session_title") or "chat"
        role = h.get("role") or "user"
        lines.append(f"- [{title}] {role}: {content}")
    if not lines:
        return ""
    return ("RELEVANT PRIOR CHAT SESSIONS — things you discussed with the user "
            "in OTHER conversations that may bear on this request (cite them if "
            "you use them):\n" + "\n".join(lines))


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
        p = _cached_find_by_source(f"repo:{repo}")
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


def _fire_stop(reason: str, cwd: str) -> None:
    """Best-effort Stop lifecycle hook at a terminal loop exit. Soft-fail: a
    hooks error must never break the turn's clean shutdown."""
    try:
        from aiforge_core.runtime import hooks as _hooks
        _hooks.fire("Stop", {"reason": reason}, cwd)
    except Exception:  # noqa: BLE001
        pass


def run_chat_agent(
    messages: list[dict], *,
    cwd: str,
    role: str = "doer",
    max_steps: int | None = None,   # kept for callers/tests; None = no cap
    complete_fn: Callable[..., str] | None = None,
    session_id: int | None = None,
    mode: str = "act",              # "act" = full tools; "plan" = read-only
    scope_globs: list[str] | None = None,  # autonomous Doer scope allowlist
    builder: str | None = None,     # job|skill|workflow|rule — task charter
    strict_finish: bool = False,    # work-producing run (doer): an IMPLICIT
    #                                 bare-prose final is premature narration →
    #                                 nudge to act, don't quit with no work done
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
    # Scope allowlist (autonomous Doer). When the caller passes globs, a
    # mutating file tool whose target path falls outside them is rejected
    # BEFORE it runs — the FunctionNode text Doer can't carry the native
    # scope_guard before_tool_callback, so this is its equivalent jail.
    # Empty/None = no restriction (back-compat; the chat UI passes nothing).
    _scope_globs = [g for g in (scope_globs or [])
                    if isinstance(g, str) and g]

    import collections
    safety = max_steps or int(os.environ.get("AIFORGE_CHAT_SAFETY_CAP", "2000"))
    # Wall-clock turn backstop. The 2000-step cap is not a real stopping
    # point on a slow local model — 2000 steps × seconds-to-minutes each is
    # effectively "forever" from the user's chair. This deadline bounds the
    # WHOLE turn regardless of step count, so a wandering or churning agent
    # (evades the exact-repeat stall guards below by varying its args) can't
    # run for hours. Generous default (1h) so it's a backstop, not a normal
    # limit; 0 disables. Tunable via AIFORGE_CHAT_TURN_DEADLINE_S.
    try:
        _turn_budget_s = float(os.environ.get("AIFORGE_CHAT_TURN_DEADLINE_S", "3600"))
    except (TypeError, ValueError):
        _turn_budget_s = 3600.0
    _turn_deadline = (time.monotonic() + _turn_budget_s) if _turn_budget_s > 0 else None

    # Latest user message drives mentions (#4) + microagent triggers (#6) +
    # memory recall. In simple/plan mode the API augments the last user turn
    # with an "[Interpreted request …]" enhancer block; key off the user's RAW
    # words (split that marker off) so recall/skills/mentions aren't diluted by
    # the boilerplate + restatement.
    last_user = next(
        (_text_of(m) for m in reversed(messages)
         if (m.get("role") or "user") == "user" and m.get("content")), "")
    # _text_of flattens a multimodal (vision) turn's list content to text, so the
    # .split() below can't crash on a list.
    last_user = last_user.split("\n\n---\n[Interpreted request")[0].strip() or last_user

    # Inject a fresh repo map every turn so the agent ALWAYS knows the
    # directory structure of the working dir without re-searching it on
    # each follow-up question (the conversation history only carries prior
    # answers, not the structure it discovered last turn).
    cave = _cave_mode()
    rules = _rules_context(cwd, last_user)
    prefs = _preferences_context(cwd)
    sys_msg = _SYSTEM.format(cwd=cwd)
    # Multi-part message (simple mode has no enhancer/spec, so nothing else
    # tracks the parts): derive an ASK CHECKLIST and pin it HIGH in the
    # system prompt — the model must cover every part, not answer #1 and stop.
    _asks = [] if builder else _split_asks(last_user)
    if _asks:
        sys_msg = ("MULTI-PART REQUEST — the user's CURRENT message contains "
                   f"{len(_asks)} distinct asks. Address EVERY one; number "
                   "your final answer to match. Checklist:\n"
                   + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
                   + "\nTRACK your progress: when you START part N call "
                     'ACTION: plan_progress ARGS_JSON: {"slug": "part-N", '
                     '"status": "running"}, and when it is DONE call it again '
                     'with "status": "done" — the user watches this live.'
                   + "\n\n" + sys_msg)
    if prefs:                       # standing user preferences — always applied
        sys_msg = prefs + "\n\n" + sys_msg
    if rules:                       # user rule book first — highest priority
        sys_msg = rules + "\n\n" + sys_msg
    if plan_mode:                   # plan banner second — constrains this turn
        sys_msg = _PLAN_BANNER + "\n\n" + sys_msg
    if builder:                     # task-specific builder charter (highest)
        try:
            from aiforge_core.runtime.prompts_extended import builders as _bld
            _charter = _bld.charter_for(builder)
        except Exception:  # noqa: BLE001 — a bad charter must never break chat
            _charter = None
        if _charter:
            sys_msg = _charter + "\n\n" + sys_msg
    # C2: budget the (un-condensable) system prompt. The CORE prompt + rules
    # above are ALWAYS kept; each optional block below is appended via a
    # budget-aware helper that truncates/drops it (lowest priority = appended
    # last = dropped first) when it would blow the cap. `_cap_system_prompt`
    # is the final backstop guaranteeing len(sys_msg) <= cap.
    _sys_cap = _sys_prompt_budget_chars(role)
    _sys_core_len = len(sys_msg)
    _sys_dropped: list[str] = []

    def _add_sys_block(label: str, block: str) -> None:
        nonlocal sys_msg
        if not block:
            return
        addition = "\n\n" + block
        if len(sys_msg) + len(addition) <= _sys_cap:
            sys_msg += addition
            return
        room = _sys_cap - len(sys_msg)
        if room > 400:              # enough left for a meaningful truncated slice
            sys_msg += addition[:room] + "\n…(truncated to fit context)\n"
        _sys_dropped.append(label)

    # Dynamic context blocks — via the SHARED bundle builder (same source
    # selection/scoping/gating as chat-team + the pipeline). rules+prefs are
    # already injected above as high-priority blocks, so skip them here.
    from aiforge_core.runtime import context_bundle as _cb
    # Proactive-recall mode. "lite" (default): send a SMALL anchor (repo summary
    # + the compacted project brief) and let the model PULL specifics via the
    # memory tools on demand — instead of pre-dumping the full recall every turn.
    # "full": the old behaviour (dump memory_md + prior-session recall upfront).
    # EXCEPTION even in lite: the SESSION-START turn injects one recall keyed to
    # the opening request, so the agent arrives informed (self-learning) instead
    # of re-deriving what past sessions worked out.
    _proactive = os.environ.get(
        "AIFORGE_CHAT_PROACTIVE_RECALL", "lite").strip().lower()
    _is_init = not any(m.get("role") == "assistant" for m in messages)
    # In lite mode a FOLLOW-UP turn doesn't inject recall at all — skip the
    # unified_query work too instead of building a block that gets dropped.
    _recall_wanted = _proactive == "full" or _is_init
    _bundle = _cb.build_bundle(
        cwd, last_user, cave=cave,
        ctx_on=lambda b: _ctx_on(b) and (b != "recall" or _recall_wanted),
        session_id=session_id, want_rules=False, want_prefs=False)
    # Project memory (compacted per-repo brief) — small + high-value; the
    # "you already know this repo" anchor. Always injected.
    _add_sys_block("project-memory", _bundle.project_brief_md)
    if _ctx_on("summary"):
        _add_sys_block("repo-summary", _bundle.repo_summary_md)
    # WORKFLOWS before the (big) repo-map, and NOT skipped in cave mode: a
    # matched workflow is a MANDATORY user procedure (branch/MR conventions,
    # naming) — dropping it silently made the agent e.g. commit straight to
    # main. Append order = drop order under a tight window, so procedures
    # must outrank the repo-map (the agent can always grep structure back).
    if _ctx_on("workflows"):
        _add_sys_block("workflows", _bundle.workflows_md)
    if not cave and _ctx_on("skills"):
        _add_sys_block("skills", _bundle.skills_md)
    if _ctx_on("repomap"):
        _add_sys_block("repo-map", _bundle.repo_map_md)
    # @-mentions — optional; cave mode skips (searchable on demand).
    if not cave and _ctx_on("mentions"):
        try:
            from aiforge_core.runtime import mentions as _mentions
            ment_block, _toks = _mentions.expand(last_user, cwd)
            _add_sys_block("mentions", ment_block)
        except Exception:  # noqa: BLE001
            pass
    # Self-learning recall — EVERY turn, keyed to the CURRENT user message
    # (from the shared bundle). Cave mode pulls fewer hits.
    if _ctx_on("recall") and _proactive == "full":
        _add_sys_block("recall", _bundle.memory_md)
        # Prior CHAT SESSIONS — surface what the user discussed in OTHER
        # conversations (excludes the current session). Cave mode → fewer hits.
        # Local SQLite scan, so cheap enough to run every turn there IS a query.
        if last_user:
            _add_sys_block("chat-recall", _chat_session_recall(
                last_user, session_id, limit=(2 if cave else 4)))
    elif _ctx_on("recall"):
        # LITE (default): don't pre-dump on follow-ups — but the SESSION-START
        # turn still gets the one-time recall keyed to the opening request.
        if _is_init:
            _add_sys_block("recall", _bundle.memory_md)
        # Tell the model it HAS memory + the tools to reach it, so it pulls
        # only what THIS turn needs.
        _add_sys_block("memory-tools",
            "MEMORY: a project brief for this repo is above. For anything "
            "specific you don't already see — past decisions/learnings, code, "
            "symbols, or what was discussed in earlier chats — CALL the tools: "
            "memory_lookup(query) for learnings/decisions, graphify_lookup for "
            "concept-graph, grep/repo_map/read for code, search_chat_sessions "
            "for prior chats. Look it up; don't guess or assume it's absent.")
    # SESSION IMAGES: descriptions of images the user attached, so the (maybe
    # text-only) model can answer questions about them all session long.
    _img_blocks: list[dict] = []
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_media
            _add_sys_block("images", chat_media.context_block(session_id))
            _img_blocks = chat_media.image_blocks_for_turn(session_id, role)
        except Exception:  # noqa: BLE001 — images must never break a turn
            _img_blocks = []
    if _sys_dropped:                # one-line note so the trim is visible
        _add_sys_block("_note", "[context note: dropped/trimmed lower-priority "
                       "blocks to fit the window: " + ", ".join(_sys_dropped) + "]")
    # A dropped WORKFLOWS/SKILLS block means the agent may skip a mandatory
    # user procedure (e.g. branch-then-MR) — surface that to the USER instead
    # of failing silently inside the prompt.
    _dropped_playbooks = [b for b in ("workflows", "skills") if b in _sys_dropped]
    # Final backstop: guarantee the system prompt is under the cap (keeps the
    # core + rules at the front; truncates the injected tail).
    sys_msg = _cap_system_prompt(sys_msg, _sys_cap, protect=_sys_core_len)
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
    continue_nudges = 0   # consecutive "narrated but didn't act" re-prompts

    # Mid-run steering (simple mode): let the user type WHILE the agent works —
    # each message is folded into the conversation as a live instruction the next
    # step must honour (parity with the pipeline's steering).
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_interject as _ci
            _ci.set_steerable(session_id, True)
        except Exception:  # noqa: BLE001
            pass

    if _dropped_playbooks:
        yield {"type": "thought", "role": "system",
               "text": "⚠ context window too small — dropped the "
                       + " + ".join(_dropped_playbooks) + " block(s): matched "
                       "workflows/skills may NOT be followed this turn. Load "
                       "the model at a larger context window to fix this."}
    # Simple-mode task tracking: surface the derived checklist in the UI's
    # subtasks dock (same events the pipeline uses) so the user can watch the
    # parts get worked live — the agent flips them via plan_progress.
    if _asks:
        yield {"type": "subtasks", "items": [
            {"slug": f"part-{i + 1}", "title": a, "status": "pending"}
            for i, a in enumerate(_asks)]}

    n = 0
    _builder_nudged = False
    _builder_finalized = False
    _builder_final_tries = 0
    _multiask_checked = False   # one-time FINAL completeness gate (multi-ask)
    while n < safety:
        n += 1
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            yield {"type": "error", "text": "stopped by user"}
            yield {"type": "done"}
            return
        # Builder nudge (#7): a local model can interview forever and never emit
        # the finalize tool, leaving the session with no artifact. Once it has had
        # enough back-and-forth, inject a one-time reminder to finalize NOW.
        if builder and not _builder_nudged and n >= _BUILDER_NUDGE_AFTER:
            _builder_nudged = True
            _fin = _BUILDER_FINALIZE_TOOL.get(builder, "the finalize tool")
            convo.append({"role": "user", "content":
                f"[system reminder] You have gathered enough detail. Call "
                f"`{_fin}` NOW with the collected values to finish — do not keep "
                f"asking questions. If one required value is genuinely missing, "
                f"ask ONLY for that, then finalize."})
        # (#16) Mid-run steering is drained in ONE place — the guarded block just
        # below (before the model call). A second, earlier drain here used to win
        # the race and append an UNGUARDED user turn, creating two consecutive
        # user turns (breaks claude_local) — removed.
        if _turn_deadline is not None and time.monotonic() > _turn_deadline:
            _fire_stop("deadline", cwd)
            yield {"type": "message",
                   "text": f"(stopped: hit the {int(_turn_budget_s)}s turn "
                           "time budget — raise AIFORGE_CHAT_TURN_DEADLINE_S "
                           "if this was real long-running work)"}
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
        convo = _compact_convo(convo, role=role, complete_fn=complete_fn,
                               session_id=session_id)
        if len(convo) < _before and not condensed_notified:
            condensed_notified = True   # notify ONCE, not every over-budget turn
            yield {"type": "thought", "role": "system",
                   "text": "⚙ condensed earlier context to stay within the window"}
        # M3: surface how full the context window is (char-estimate; ~4 chars/
        # token) so the user can see they're approaching the condense point.
        # MUST mirror _compact_convo's math exactly (history-only sum vs a
        # budget that reserves the ACTUAL system prompt, list-safe _text_of) —
        # the old raw-len/whole-convo version double-counted the per-turn
        # system prompt against a 14K estimate, so the meter jumped between
        # turns and collapsed to ~0 on image turns.
        _sys_len = (len(_text_of(convo[0]))
                    if convo and convo[0].get("role") == "system" else 0)
        _ctx_chars = sum(len(_text_of(m)) for m in convo[1:])
        _ctx_budget = _ctx_budget_chars(role, sys_chars=_sys_len)
        if _ctx_budget > 0:
            # ~4 chars/token → surface ABSOLUTE token counts (in k) alongside the
            # pct so the UI can show "120k / 256k" not just a bare percentage.
            _ctx_tokens = _ctx_chars // 4
            _win_tokens = _ctx_budget // 4
            yield {"type": "usage", "context_chars": _ctx_chars,
                   "budget_chars": _ctx_budget,
                   "context_tokens": _ctx_tokens,
                   "window_tokens": _win_tokens,
                   "pct": min(100, round(_ctx_chars * 100 / _ctx_budget))}
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
        except Exception as exc:  # noqa: BLE001
            # RESILIENCE: a local model can transiently drop a request (mid-load,
            # busy, a one-off empty/4xx). Retry a few times before surfacing, and
            # never show the raw `llm.exhausted role=chat …` stack; give a plain,
            # actionable message.
            # AIFORGE_CHAT_LLM_RETRIES tunes the retry count (default 5) — a
            # local model that's loading/busy often needs a few passes.
            _retries = 5
            try:
                _retries = max(0, int(os.environ.get("AIFORGE_CHAT_LLM_RETRIES", "5")))
            except ValueError:
                _retries = 5
            out = None
            _last = exc
            for _rn in range(_retries):
                if session_id is not None and chat_cancel.is_cancelled(session_id):
                    break
                yield {"type": "thought", "role": "system",
                       "text": f"⟳ model didn't respond — retrying ({_rn + 1}/{_retries})…"}
                # Escalating backoff: give a mid-load / busy local model (or a
                # slow compress+forward hop) progressively more room to recover.
                time.sleep(3.0 * (_rn + 1))
                try:
                    out = _complete_cancellable(complete_fn, role, convo, session_id)
                    _last = None
                    break
                except Exception as exc2:  # noqa: BLE001
                    _last = exc2
            if _last is not None:
                yield {"type": "message", "text":
                       "⚠️ The model didn't respond (it may be loading, busy, or the "
                       "request was rejected). Nothing was changed — please try again "
                       "in a moment. If it keeps happening, check the model endpoint."}
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
            # In a builder session, a "final" BEFORE the finalize tool succeeded
            # means the model narrated/stalled ("let me test what's happening…")
            # instead of building the artifact — don't end the interview with
            # nothing created. Nudge it to call the finalize tool and continue the
            # loop (bounded so a model that truly can't finalize still exits).
            if builder and not _builder_finalized and _builder_final_tries < 2:
                _builder_final_tries += 1
                _fin = _BUILDER_FINALIZE_TOOL.get(builder, "the finalize tool")
                if step.get("text"):
                    yield {"type": "thought", "text": step["text"]}
                convo.append({"role": "user", "content":
                    f"[system reminder] You stopped without creating the {builder}. "
                    f"Call `{_fin}` NOW with the collected values to finish — do "
                    f"not just narrate or 'test'. If ONE required value is genuinely "
                    f"missing, ask only for that, then finalize."})
                continue
            # Doer guard: an IMPLICIT final (bare prose, no explicit `FINAL:`
            # marker) from a work-producing run (strict_finish — the text-doer /
            # subtask path) is almost always premature narration ("let me test…"),
            # not a real answer. Nudge to act/finish instead of ending with no work.
            # Bounded by continue_nudges so a model that truly can't finish still
            # exits. Interactive chat / generic callers (strict_finish=False) keep
            # bare prose as the legitimate answer — unchanged.
            if step.get("implicit") and strict_finish and not builder:
                continue_nudges += 1
                if continue_nudges <= 2:
                    if step.get("text"):
                        yield {"type": "thought", "text": step["text"]}
                    convo.append({"role": "user", "content":
                        "You narrated but did NOT emit an ACTION or an explicit "
                        "`FINAL:` line. Continue: take the next ACTION (tool call) "
                        "to make progress, or output `FINAL: <answer>` ONLY when "
                        "the work is actually done. Do not just narrate or 'test'."})
                    continue
            # Multi-ask completeness gate (once): before accepting FINAL on a
            # multi-part message, make the model self-check its answer against
            # the checklist — the #1 simple-mode complaint is answering ask 1
            # and silently dropping the rest.
            if _asks and not _multiask_checked and not builder:
                _multiask_checked = True
                yield {"type": "thought", "role": "system",
                       "text": f"✔ checking all {len(_asks)} parts of the "
                               "request are addressed…"}
                convo.append({"role": "user", "content":
                    "[completeness check — not the user] The user's message "
                    f"contained {len(_asks)} distinct asks:\n"
                    + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
                    + "\nRe-read your answer above. If EVERY ask is addressed, "
                    "resend it unchanged as FINAL. If any is missing, do the "
                    "missing work now (ACTIONs as needed) and produce ONE "
                    "complete FINAL covering all parts, numbered."})
                continue
            # FINAL accepted on a multi-part turn: close out the tracker so
            # the dock never ends with stale pending items the model forgot
            # to flip.
            if _asks:
                for _i in range(len(_asks)):
                    yield {"type": "subtask_update",
                           "slug": f"part-{_i + 1}", "status": "done"}
            _fire_stop("final", cwd)
            yield {"type": "message", "text": _strip_reasoning_prefix(step["text"])}
            yield {"type": "done"}
            return
        if step["kind"] == "ask":
            # Agent is asking the user a question — show it + wait for the
            # next message (which answers it). awaiting_input flags the UI.
            yield {"type": "message", "awaiting_input": True,
                   "text": step["text"]}
            yield {"type": "done"}
            return

        if step["kind"] == "continue":
            # The model narrated a next step (THOUGHT) but emitted no ACTION —
            # usually a truncated turn or a dropped protocol line. Surface the
            # thought and nudge it to actually act, instead of ending the run.
            if step.get("thought"):
                yield {"type": "thought", "text": step["thought"]}
            continue_nudges += 1
            if continue_nudges > 2:
                # It keeps describing without acting — stop cleanly rather than
                # loop to the safety cap; hand back what it was thinking.
                _fire_stop("no_action", cwd)
                yield {"type": "message",
                       "text": (step.get("thought") or "").strip()
                       or "I described a next step but couldn't complete the "
                          "action. Could you rephrase or narrow the request?"}
                yield {"type": "done"}
                return
            convo.append({"role": "user",
                          "content": "You described your next step but did NOT "
                          "emit an ACTION. Continue now — output the next ACTION "
                          "(tool call) to make progress, or `FINAL: <answer>` if "
                          "you are genuinely done. Do not just narrate."})
            n += 1
            continue

        continue_nudges = 0   # a real action resets the narration guard

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

        # Simple-mode task tracker: plan_progress flips a checklist item in
        # the UI's subtasks dock. Pure bookkeeping — no side effects, allowed
        # in every mode (incl. plan), never gated.
        if name == "plan_progress":
            _slug = str(args.get("slug") or args.get("part") or "").strip()
            _st = str(args.get("status") or "done").strip().lower()
            if _st not in ("pending", "running", "done", "failed"):
                _st = "done"
            if _slug:
                yield {"type": "subtask_update", "slug": _slug, "status": _st}
            result = {"ok": bool(_slug), "slug": _slug, "status": _st,
                      **({} if _slug else {"error": "missing 'slug'"})}
            yield {"type": "tool", "name": name, "args": args, "result": result}
            convo.append({"role": "user",
                          "content": f"OBSERVATION: {json.dumps(result)}"})
            continue

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
                # Autonomous path (parallel sub-Doer) — no human to approve.
                # Mirror run_shell's floor: auto-approve caution/review gates,
                # hard-block only truly DANGEROUS commands + destructive deletes
                # (a blanket reject here silently broke sudo / -g installs /
                # force-push in worktree-isolated autonomous runs).
                _danger = bool(_destructive_del)
                if not _danger and name in ("run_command", "run_shell", "serve",
                                            "bash", "shell"):
                    try:
                        from aiforge_core.runtime.tools import command_risk
                        _lvl = command_risk.assess(
                            args.get("cmd") or args.get("command") or "")["level"]
                        _danger = _lvl == command_risk.DANGEROUS
                    except Exception:  # noqa: BLE001
                        _danger = False
                decision = ({"decision": "reject", "note": "autonomous: dangerous action blocked"}
                            if _danger else
                            {"decision": "approve", "note": "autonomous auto-approve"})
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
            # A Stop that landed WHILE the approval gate was open must not still
            # write the file — the file tools have no subprocess for cancel() to
            # kill, so re-check here before dispatching the (now-approved) tool.
            if session_id is not None and chat_cancel.is_cancelled(session_id):
                yield {"type": "tool", "name": name, "args": args,
                       "result": {"ok": False, "error": "cancelled"}}
                # continue (not break) → the top-of-loop cancel check emits the
                # accurate "stopped by user" rather than the safety-cap message.
                continue

        # Lifecycle hook (Claude Code parity): PreToolUse can block a tool
        # (a `block_on_nonzero` hook that exits non-zero) — surface it like the
        # plan-mode/policy blocks. Hooks soft-fail; a hooks error never breaks
        # the turn.
        _hook_block = None
        try:
            from aiforge_core.runtime import hooks as _hooks
            _pre = _hooks.fire("PreToolUse", {"tool": name, "args": args}, cwd)
            if _pre.get("blocked"):
                _hook_block = _pre
        except Exception:  # noqa: BLE001 — hooks must never break dispatch
            _hook_block = None

        # Scope allowlist enforcement (autonomous Doer path). Reject a
        # mutating file tool whose resolved target path is outside the
        # ticket's scope_allowlist_globs — refuse WITHOUT writing, and hand
        # the model a corrective observation. Reuses scope_guard's matcher
        # so the text path enforces exactly like the native callback.
        if _scope_globs:
            try:
                from aiforge_core.runtime import scope_guard as _sg
                _off = [p for p in _sg._path_from_args(name, args or {})
                        if not _sg._matches_any(p, _scope_globs)]
            except Exception:  # noqa: BLE001 — never break dispatch
                _off = []
            if _off:
                result = {
                    "ok": False, "error": "scope_violation",
                    "blocked_paths": _off,
                    "scope_allowlist_globs": _scope_globs,
                    "hint": ("Edit refused: path is outside the ticket's "
                             "scope_allowlist_globs. Edit only files inside "
                             "an allowed glob."),
                }
                yield {"type": "tool", "name": name, "args": args,
                       "result": result}
                convo.append({"role": "user",
                              "content": f"OBSERVATION: {json.dumps(result)}"})
                continue

        fn = TOOLS.get(name)
        if _hook_block is not None:
            result = {"ok": False, "blocked": "hook", "hook": _hook_block,
                      "error": f"'{name}' was blocked by a PreToolUse hook"}
        elif fn is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            # Live "it's running" signal — a slow tool (bash/test/build) used
            # to show NOTHING until `fn` returned, so the UI looked stalled
            # for however long the command actually took. `call_id` (the
            # ReAct step counter `n`, unique per iteration) lets the UI match
            # this to the completed `tool` event below and flip it in place
            # instead of appending a second, duplicate row.
            yield {"type": "tool_start", "name": name, "args": args,
                   "call_id": n}
            _perf_t0 = time.perf_counter()
            # Strong tools resolve through sandbox.root(); scope the override to
            # the workspace root (NOT the raw cwd, so it can't escape an
            # AIFORGE_WORKSPACE_DIR jail) and ALWAYS reset it in finally so a
            # reused thread can't leak this session's dir into the next.
            _root_tok = None
            if name in _ROOT_SCOPED_TOOLS:
                try:
                    from aiforge_core.runtime import sandbox as _sb
                    _root_tok = _sb.set_root_override(_scoped_root(cwd))
                except Exception:  # noqa: BLE001
                    _root_tok = None
            try:
                result = fn(args, cwd)
            except KeyError as exc:
                result = {"ok": False, "error": f"missing arg: {exc}"}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            finally:
                if _root_tok is not None:
                    try:
                        from aiforge_core.runtime import sandbox as _sb
                        _sb.reset_root_override(_root_tok)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                from aiforge_core.runtime import perf_recorder
                perf_recorder.record(
                    _perf_family(name), name,
                    (time.perf_counter() - _perf_t0) * 1000.0)
            except Exception:  # noqa: BLE001 — perf must never break a run
                pass
        # PostToolUse hook (best-effort, never blocks).
        try:
            from aiforge_core.runtime import hooks as _hooks
            _hooks.fire("PostToolUse",
                        {"tool": name, "args": args, "result": result}, cwd)
        except Exception:  # noqa: BLE001 — hooks must never break the turn
            pass
        yield {"type": "tool", "name": name, "args": args, "result": result,
               "call_id": n}
        # Builder finalize: a successful create_job_script / learn_skill /
        # learn_workflow / remember_rule ends the interview. Signal the UI so it
        # can drop this session's builder mode — otherwise every later message
        # re-fires the charter and the user is stuck building forever (and can be
        # walked into duplicate artifacts).
        if name in _FINALIZE_TOOLS and isinstance(result, dict) and result.get("ok"):
            _builder_finalized = True
            yield {"type": "builder_done", "kind": name}
        _obs_cap = _MAX_OBS_READ if name in _READ_OBS_TOOLS else _MAX_OBS
        # Content-READ tools: cut oversized documents at a STRUCTURE boundary
        # (chonkie) with a continuation note, instead of a blunt slice that
        # hands the model a broken JSON/sentence tail. Others keep the slice.
        obs = (_smart_truncate_obs(result, _obs_cap)
               if name in _READ_OBS_TOOLS else json.dumps(result)[:_obs_cap])
        # Recency reminder: a strict output format from an APPLICABLE SKILL sits
        # in the system prompt (far above), while this fresh tool result sits at
        # the end where the model attends most — so after a tool round-trip it
        # tends to summarize the result in its own words and drop the format
        # (e.g. a jira-reading skill's exact layout). Re-assert the format right
        # next to the data so the FINAL honours it. Only when a skill fired.
        _tail = ("\n[format reminder] If your FINAL presents this result and an "
                 "APPLICABLE SKILL above specifies an output format, reproduce "
                 "it EXACTLY — no extra prose, headers, or table it does not "
                 "specify.") if _bundle.skills_md else ""
        convo.append({"role": "user", "content": f"OBSERVATION: {obs}{_tail}"})

    _fire_stop("cap", cwd)
    yield {"type": "message",
           "text": "(stopped: hit the runaway safety cap — "
                   "raise AIFORGE_CHAT_SAFETY_CAP if this was real work)"}
    yield {"type": "done"}
