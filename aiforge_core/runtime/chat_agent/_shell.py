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


