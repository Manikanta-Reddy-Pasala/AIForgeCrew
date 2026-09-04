from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

_BASH = '.bash'

_ACTION_RE = re.compile(r"ACTION:\s*([A-Z_]+)", re.IGNORECASE)
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
    "jira_dashboard_read", "jira_myself", "file_read", "read_files", "read_lines",
    "gitlab_read", "web_fetch", "web_crawl", "email_read",
    # A CI pipeline's answer is a job log tail. Under the blunt 6k cap the
    # `jobs` array alone pushed it out of the observation — at 14 jobs,
    # measured — so the model read truncated JSON and no failure reason.
    "gitlab_pipeline", "gitlab_pipelines", "gitlab_pipeline_watch",
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
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# Split a (quote/heredoc-masked) command into simple-command chunks on every
# shell separator AND subshell/group punctuation, so a blanket stage nested in
# ``(...)`` / ``{...}`` — including the no-separator ``(git add -A)`` form — is
# still seen.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|()\{\}\n]")


class _NonCodeMasker:
    """Blanks quoted-string and heredoc-body regions, preserving length.

    One method per shell construct. It was a single while-loop with the scanner
    for every construct inlined, which is what made an ordinary lexer hard to
    read: each branch is small, they simply all shared ``i``.
    """

    _DELIM_RE = re.compile(r"""(["']?)([A-Za-z_]\w*)\1""")

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd
        self.n = len(cmd)
        self.out = list(cmd)
        self.i = 0
        self.pending: list[tuple[str, bool]] = []   # (delimiter, strip-tabs)

    def _blank(self, a: int, b: int) -> None:
        for k in range(a, min(b, self.n)):
            if self.out[k] != "\n":
                self.out[k] = " "

    def _single_quote(self) -> None:
        j = self.cmd.find("'", self.i + 1)
        end = (j + 1) if j != -1 else self.n
        self._blank(self.i, end)
        self.i = end

    def _double_quote(self) -> None:
        j = self.i + 1
        while j < self.n:
            if self.cmd[j] == "\\":
                j += 2
                continue
            if self.cmd[j] == '"':
                break
            j += 1
        end = (j + 1) if j < self.n else self.n
        self._blank(self.i, end)
        self.i = end

    def _blank_one_body(self, delim: str, strip_tabs: bool) -> None:
        """Blank lines until the delimiter line, which ends this heredoc."""
        while self.i < self.n:
            eol = self.cmd.find("\n", self.i)
            line_end = eol if eol != -1 else self.n
            line = self.cmd[self.i:line_end]
            self._blank(self.i, line_end)
            self.i = line_end + 1 if eol != -1 else self.n
            cmp_line = line.lstrip("\t") if strip_tabs else line
            if cmp_line.strip() == delim:
                return

    def _heredoc_bodies(self) -> None:
        """At the newline that opens the queued heredocs, in declared order."""
        self.i += 1
        for delim, strip_tabs in self.pending:
            self._blank_one_body(delim, strip_tabs)
        self.pending = []

    def _heredoc_start(self) -> None:
        """Queue a ``<<DELIM`` / ``<<-DELIM``; its body starts at the newline."""
        op = re.match(r"<<-?", self.cmd[self.i:]).group(0)
        self.i += len(op)
        while self.i < self.n and self.cmd[self.i] in " \t":
            self.i += 1
        dm = self._DELIM_RE.match(self.cmd[self.i:])
        if dm:
            self.pending.append((dm.group(2), op.endswith("-")))
            self.i += dm.end()

    def run(self) -> str:
        while self.i < self.n:
            ch = self.cmd[self.i]
            if ch == "\n" and self.pending:
                self._heredoc_bodies()
            elif ch == "'":
                self._single_quote()
            elif ch == '"':
                self._double_quote()
            elif ch == "\\":
                self.i += 2                      # escaped char is never code
            elif self.cmd[self.i:self.i + 2] == "<<":
                self._heredoc_start()
            else:
                self.i += 1
        return "".join(self.out)


def _mask_noncode(cmd: str) -> str:
    """Return ``cmd`` with quoted-string and heredoc-body regions blanked to
    spaces (length-preserving), so a ``git add -A`` living inside ``echo "…"``
    or a heredoc body is NOT scanned as a real command."""
    return _NonCodeMasker(cmd).run()


def _peel_benign_prefixes(toks: list[str]) -> list[str]:
    """Drop leading ``env=val`` assignments and ``sudo`` (with its dash-flags),
    so ``sudo git add -A`` / ``FOO=bar git add -A`` are not mistaken for a
    non-git command."""
    k = 0
    while k < len(toks):
        if _ENV_ASSIGN_RE.match(toks[k]):
            k += 1
        elif toks[k] == "sudo":
            k += 1
            while k < len(toks) and toks[k].startswith("-"):
                k += 1
        else:
            break
    return toks[k:]


def _git_subcommand(toks: list[str]) -> tuple[str, list[str]] | None:
    """``(subcommand, args)`` for a git invocation, past its ``-C <dir>``-style
    globals; None when the segment is not git at all."""
    toks = _peel_benign_prefixes(toks)
    if len(toks) < 2 or toks[0] != "git":
        return None
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in _GIT_GLOBAL_VALUE_OPTS else 1
    return (toks[i], toks[i + 1:]) if i < len(toks) else None


def _is_blanket_add(after: list[str]) -> bool:
    """``git add -A`` / ``.`` / ``--all`` in any form, and NOTHING targeted:
    one blanket selector, and every other argument a selector or ``--``."""
    allowed = _BLANKET_ADD_SELECTORS | {"--"}
    return bool(after) and all(a in allowed for a in after) \
        and any(a in _BLANKET_ADD_SELECTORS for a in after)


def _is_autostage_commit(after: list[str]) -> bool:
    """``git commit --all``, or an ``a`` inside a short flag cluster (-a, -am)."""
    return any(a == "--all"
               or (a.startswith("-") and not a.startswith("--") and "a" in a)
               for a in after)


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
        found = _git_subcommand(seg.split())
        if found is None:
            continue
        sub, after = found
        if sub == "add" and _is_blanket_add(after):
            return True
        if sub == "commit" and _is_autostage_commit(after):
            return True
    return False


# ─── Chat-side: REFUSE a foreground server-start (run it via `serve`) ─────────
#
# ``run_command`` polls the process until it exits or the timeout, so a
# long-lived server launcher (``./run.sh``, ``npm run dev``, ``uvicorn`` …) that
# never returns WEDGES the whole turn for up to AIFORGE_CHAT_CMD_TIMEOUT_S
# (default 600s) — the source of the chat "Agent error: network error" bug (the
# request sits open on a self-conflicting server-start and the browser drops
# it). The `serve` tool exists exactly for this: it detaches the process and
# returns immediately with the bound URL. So we REFUSE a blocking server-start
# (fail-CLOSED) and redirect the model to `serve`; the loop turns the refusal
# into an observation. Escape hatch: append ` &` to background it yourself.

# Programs whose bare invocation is a long-lived server/dev process.
_SERVER_PROGRAMS = frozenset({
    "uvicorn", "gunicorn", "hypercorn", "daphne", "nodemon", "vite",
    "http-server", "serve", "caddy", "honcho", "foreman",
    "webpack-dev-server", "webpack-serve"})
_NODE_PMS = frozenset({"npm", "pnpm", "yarn", "bun"})
# npm/pnpm/yarn/bun sub-commands that run a dev server (vs one-shot build/test).
_NODE_SERVER_SUBS = frozenset({"dev", "start", "serve"})
# Python ``-m`` modules that ARE a server.
_SERVER_PY_MODULES = frozenset({
    "http.server", "uvicorn", "gunicorn", "hypercorn", "daphne", "waitress"})
# Benign leading words to peel before the real program (like sudo/env for git).
_SERVER_PREFIX_WORDS = frozenset({"sudo", "nohup", "exec", "time", "command"})


def _peel_prefixes(toks: list[str]) -> list[str]:
    """Drop leading ``env=val`` assignments + benign prefix words (sudo/nohup/
    exec/time, with their dash-flags) so the real program surfaces — the same
    peel :func:`_is_blanket_git` does, shared here (DRY)."""
    k = 0
    while k < len(toks):
        t = toks[k]
        if _ENV_ASSIGN_RE.match(t):
            k += 1
            continue
        if t in _SERVER_PREFIX_WORDS:
            k += 1
            while k < len(toks) and toks[k].startswith("-"):
                k += 1
            continue
        break
    return toks[k:]


def _node_pm_starts_server(rest: list[str]) -> bool:
    """``npm run dev`` / ``pnpm run serve`` / bare ``yarn dev``."""
    args = [a for a in rest if not a.startswith("-")]
    if not args:
        return False
    if args[0] == "run":
        return len(args) > 1 and args[1] in _NODE_SERVER_SUBS
    return args[0] in _NODE_SERVER_SUBS


def _python_starts_server(rest: list[str]) -> bool:
    """``python -m http.server`` / ``python manage.py runserver``."""
    if "-m" in rest:
        i = rest.index("-m")
        if i + 1 < len(rest) and rest[i + 1] in _SERVER_PY_MODULES:
            return True
    return "runserver" in rest


# program (basename) → does this argv start a server? Everything not listed
# falls through to the flat _SERVER_PROGRAMS set.
_SERVER_BY_PROG = {
    "flask": lambda rest: "run" in rest,
    "django-admin": lambda rest: "runserver" in rest,
    "next": lambda rest: any(a in _NODE_SERVER_SUBS for a in rest),
    "ng": lambda rest: any(a in _NODE_SERVER_SUBS for a in rest),
    "rails": lambda rest: any(a in ("server", "s") for a in rest),
    "php": lambda rest: "-S" in rest,
}


def _cmd_starts_server(toks: list[str]) -> bool:
    """True when a single (already prefix-peeled) command launches a long-lived
    server/dev process: ``npm run dev``, ``uvicorn app:app``, ``flask run``,
    ``python -m http.server``, ``rails server``, ``php -S …``, ``next dev`` …
    One-shot builds/tests/installs (``npm run build``, ``npm ci``) are NOT."""
    if not toks:
        return False
    prog = toks[0].rsplit("/", 1)[-1]
    rest = toks[1:]
    if prog in _NODE_PMS:
        return _node_pm_starts_server(rest)
    if prog in ("python", "python3", "manage.py") or any(
            a.rsplit("/", 1)[-1] == "manage.py" for a in rest):
        if _python_starts_server(rest):
            return True
    check = _SERVER_BY_PROG.get(prog)
    if check is not None:
        return check(rest)
    return prog in _SERVER_PROGRAMS


def _script_starts_server(path: str, base: str | None) -> bool:
    """True when a local shell script's CONTENT launches a server — so a
    ``./run.sh`` that execs uvicorn is flagged, while a one-shot ``run.sh`` that
    just echoes is NOT. Content-driven (not name-based) so it stays generic: no
    project-specific launcher names hardcoded. Bounded read; fails OPEN (a
    missing/unreadable script → not flagged, so the preflight can handle it)."""
    if not base:
        return False
    p = path[2:] if path.startswith("./") else path
    full = p if os.path.isabs(p) else os.path.join(base, p)
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            body = fh.read(4000)
    except (OSError, ValueError):
        return False
    for line in _mask_noncode(body).splitlines():
        if _cmd_starts_server(_peel_prefixes(line.split())):
            return True
    return False


def _script_arg(toks: list[str]) -> str | None:
    """The shell script this argv runs — ``./x.sh`` or ``bash x.sh`` — else None.
    Flagged only by its CONTENT later, never on the name alone."""
    pbase = toks[0].rsplit("/", 1)[-1]
    if pbase.endswith((".sh", _BASH)):
        return toks[0]
    if pbase in ("bash", "sh", "zsh") and len(toks) > 1 \
            and toks[1].rsplit("/", 1)[-1].endswith((".sh", _BASH)):
        return toks[1]
    return None


def _is_server_start(cmd: str, base: str | None = None) -> bool:
    """True when ``cmd`` would launch a long-lived FOREGROUND server that never
    returns, so ``run_command`` would poll it until the timeout and WEDGE the
    whole turn (the chat "network error" bug) — the ``serve`` tool should start
    it instead.

    Detects explicit server commands (``npm run dev``, ``uvicorn``, ``flask
    run``, ``python -m http.server`` …) from the string, AND a launcher SCRIPT
    (``./run.sh`` / ``bash run.sh``) by its CONTENT when ``base`` locates it —
    so a one-shot script keeps working. Quote/heredoc-aware, sees a server in an
    ``a && b`` chain, peels ``sudo``/``env=val`` prefixes. A command already
    BACKGROUNDED (trailing ``&``) returns at once, so it is NOT flagged."""
    return any(_segment_starts_server(seg, base)
               for seg in _SEGMENT_SPLIT_RE.split(_mask_noncode(cmd or "")))


def _segment_starts_server(seg: str, base: str | None) -> bool:
    """One simple-command chunk of an ``a && b`` chain."""
    seg = seg.strip()
    if not seg or seg.endswith("&"):         # backgrounded → returns; allow
        return False
    toks = _peel_prefixes(seg.split())
    if not toks:
        return False
    script = _script_arg(toks)
    if script is not None:
        return _script_starts_server(script, base)
    return _cmd_starts_server(toks)


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


def _requested_paths(args: dict) -> list[str]:
    """``paths`` as a list — it also arrives as a comma/newline string, or under
    the singular ``path`` key."""
    raw = args.get("paths")
    if raw is None:
        raw = args.get("path") or []
    if isinstance(raw, str):
        raw = [s for s in re.split(r"[,\n]+", raw) if s.strip()]
    return [str(p).strip() for p in (raw or []) if str(p).strip()]


def _read_files_cap() -> int:
    try:
        return max(200, int(os.environ.get(
            "AIFORGE_CHAT_READ_FILES_PER_CAP", "6000")))
    except ValueError:
        return 6000


def _one_read_block(path: str, cwd: str, per_cap: int) -> tuple[str, bool]:
    """``(=== path === block, ok)`` for one file, capped so no single file eats
    the observation budget."""
    r = _t_file_read({"path": path}, cwd)
    if not (isinstance(r, dict) and r.get("ok") and not r.get("is_dir")):
        err = r.get("error") if isinstance(r, dict) else "unknown error"
        return f"=== {path} ===\n[read failed: {err}]", False
    txt = str(r.get("content") or "")
    if len(txt) > per_cap:
        txt = (txt[:per_cap] + f"\n…[truncated {len(txt) - per_cap} chars — "
               "use read_lines for the rest]")
    return f"=== {path} ===\n{txt}", True


def _t_read_files(args: dict, cwd: str) -> dict:
    """Read MANY files in ONE call — the batched form of :func:`_t_file_read`.

    Local models on a long ONE-AT-A-TIME read chain lose track of what they've
    read and stall re-reading old files; batching a whole set into a single turn
    sidesteps that entirely. Accepts ``paths`` (a list, or a comma/newline
    string). Returns every file's content concatenated under ``=== path ===``
    headers in one ``content`` field, each file capped so no single file eats the
    observation budget (raise AIFORGE_CHAT_READ_FILES_PER_CAP; default 6000)."""
    paths = _requested_paths(args)
    if not paths:
        return {"ok": False, "error": "missing 'paths' (a list of file paths)"}
    per_cap = _read_files_cap()
    max_files = 60
    dropped = max(0, len(paths) - max_files)
    blocks = [_one_read_block(p, cwd, per_cap) for p in paths[:max_files]]
    ok_n = sum(1 for _, ok in blocks if ok)
    err_n = len(blocks) - ok_n
    note = f"{ok_n} read, {err_n} failed"
    if dropped:
        note += f", {dropped} skipped (>{max_files}-file cap — call again)"
    return {"ok": ok_n > 0, "count": ok_n, "read": ok_n, "failed": err_n,
            "content": "\n\n".join(b for b, _ in blocks), "note": note}


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
_SCRIPT_EXTS = (".sh", _BASH, ".py", ".js", ".mjs", ".rb", ".pl")


def _is_literal_path(p: str) -> bool:
    """A path we can check on disk — nothing dynamic ($VAR, ``, $(), globs)."""
    return not any(ch in p for ch in ("$", "`", "*", "?", "(", "{"))


def _resolved(path: str, cur: str) -> str:
    p = os.path.expanduser(path)
    return p if os.path.isabs(p) else os.path.join(cur, p)


def _script_token(toks: list[str]) -> str | None:
    """The script a segment runs — ``bash x.sh`` or ``./x.sh`` — else None."""
    head = toks[0]
    if head in _SCRIPT_RUNNERS:
        cand = next((t for t in toks[1:] if not t.startswith("-")), None)
        return cand if cand and cand.lower().endswith(_SCRIPT_EXTS) else None
    if (head.startswith("./") or os.path.isabs(head)) \
            and head.lower().endswith(_SCRIPT_EXTS):
        return head
    return None


def _segment_tokens(cmd: str):
    """Each separator-delimited segment as tokens; unparseable ones skipped
    (complex shell → fail open)."""
    import shlex
    for seg in re.split(r"&&|\|\||;|\n|\|", cmd or ""):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if toks:
            yield toks


def _script_missing_error(toks, cur):
    """Error string when a segment runs a LITERAL script path that does not
    exist under ``cur``, else None (dynamic/absent script → fail open)."""
    script = _script_token(toks)
    if script and _is_literal_path(script):
        p = _resolved(script, cur)
        if not os.path.isfile(p):
            return (f"script does not exist: {script!r} (resolved "
                    f"{os.path.normpath(p)}). Nothing was run. Find it "
                    "first (file_find / list_dir) or create it, then "
                    "re-issue.")
    return None


def _preflight_missing_path(cmd: str, base: str) -> str | None:
    """Cheap existence check BEFORE running: a `cd <dir>` into a folder that
    doesn't exist, or `bash <script>` / `./script.sh` on a missing file, fails
    with a cryptic shell error the model then thrashes on. Validate LITERAL
    paths only — anything dynamic ($VAR, ``, $(), globs) is skipped (fail-open).
    Tracks `cd` chains so `cd a && ./b.sh` checks b.sh under a/. Returns a
    human-actionable error string, or None when nothing is provably missing."""
    cur = base
    for toks in _segment_tokens(cmd):
        if toks[0] == "cd" and len(toks) >= 2:
            tgt = toks[1]
            if tgt == "-" or not _is_literal_path(tgt):
                return None            # dynamic — stop tracking, fail open
            d = _resolved(tgt, cur)
            if not os.path.isdir(d):
                return (f"cd target does not exist: {tgt!r} (resolved "
                        f"{os.path.normpath(d)}). Nothing was run. Check the "
                        "path first (list_dir / file_find) and re-issue with "
                        "the real folder.")
            cur = os.path.normpath(d)
            continue
        err = _script_missing_error(toks, cur)
        if err:
            return err
    return None


_OBS_TEXT_KEYS = ("content", "text", "body", "markdown", "preview", "stdout")


def _largest_text_key(result: dict, raw_len: int, cap: int) -> str | None:
    """The text field big enough that trimming it alone gets us under ``cap``."""
    key = max((k for k in _OBS_TEXT_KEYS if isinstance(result.get(k), str)),
              key=lambda k: len(result[k]), default=None)
    return key if key and len(result[key]) > (raw_len - cap) else None


def _cut_at_structure(text: str, budget: int) -> str:
    """``budget`` chars of ``text``, cut at a structure boundary."""
    from aiforge_core.integrations import chonkie_text_adapter
    if chonkie_text_adapter.available():
        return chonkie_text_adapter.cut_at_structure(text, budget)
    # dep-free structural fallback: last paragraph boundary
    kept = text[:budget]
    nl = kept.rfind("\n\n")
    return kept[:nl] if nl > budget // 2 else kept


def _trimmed_json(result: dict, key: str, raw_len: int, cap: int) -> str:
    text = result[key]
    budget = max(500, len(text) - (raw_len - cap) - 200)
    kept = _cut_at_structure(text, budget)
    trimmed = dict(result)
    trimmed[key] = (kept + f"\n…[TRUNCATED at a structure "
                    f"boundary — {len(kept)} of {len(text)} chars "
                    "shown. The document CONTINUES: use read_lines "
                    "with an offset, or ask for a specific section.]")
    return json.dumps(trimmed)


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
        key = _largest_text_key(result, len(raw), cap) \
            if isinstance(result, dict) else None
        if key:
            out = _trimmed_json(result, key, len(raw), cap)
            if len(out) <= cap + 400:          # small tolerance for the note
                return out
    except Exception:  # noqa: BLE001 — smart cut is best-effort
        pass
    return raw[:cap]


_BLANKET_GIT_REFUSAL = (
    "Blanket staging (git add -A / git add . / git commit -a) is disabled in "
    "chat to avoid committing unrelated files. Stage ONLY the files you "
    "changed: `git add <path1> <path2>` then `git commit -m \"...\"` then "
    "`git push`.")

_SERVER_START_REFUSAL = (
    "This starts a long-lived server/dev process that won't return, so "
    "run_command would block the whole turn. Use the `serve` tool instead — "
    "serve(cmd=\"…\") starts it in the background and gives you the URL "
    "immediately (stop it later with stop_service). If you truly want it in "
    "the foreground, append ` &` to background it yourself.")


def _run_refusal(cmd: str, args: dict, base: str) -> dict | None:
    """The pre-flight gates, all fail-CLOSED. None means the command may run.

    - a destructive delete without an explicit confirm;
    - blanket git staging, which in chat would sweep the user's UNRELATED files
      (and the agent's own artifacts) into a commit. We do NOT execute it — the
      soft error makes the agent loop re-issue a targeted `git add <paths>`;
    - a FOREGROUND server-start: it never returns, so run_command would poll it
      until the (10-min) timeout and wedge the turn — the chat "network error"
      bug. Redirected to `serve`, which backgrounds it and returns the URL;
    - a literal `cd <missing dir>` / `bash <missing script>`, refused with an
      actionable error instead of the shell's cryptic "No such file or
      directory" (which the model then thrashes on).
    """
    from aiforge_core.runtime.tools import delete_guard
    allow_delete = delete_guard.allow_delete(
        ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
    if not allow_delete and not args.get("confirm_delete") \
            and delete_guard.is_destructive_delete(cmd):
        return {"ok": False, "blocked": "delete",
                "error": delete_guard.REFUSAL + " (re-issue with "
                         "confirm_delete=true after the user agrees.)"}
    if _is_blanket_git(cmd):
        return {"ok": False, "blocked": "blanket_git",
                "error": _BLANKET_GIT_REFUSAL}
    if _is_server_start(cmd, base):
        return {"ok": False, "blocked": "server_start",
                "error": _SERVER_START_REFUSAL}
    missing = _preflight_missing_path(cmd, base)
    if missing:
        return {"ok": False, "blocked": "missing_path", "error": missing}
    return None


def _drain(proc, timeout: float = 5) -> tuple[str, str] | None:
    """Whatever the process buffered, or None if it could not be collected."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return out or "", err or ""


def _timeout_result(proc, timeout: int) -> dict:
    """Capture whatever the command buffered BEFORE we kill it, so the agent
    sees partial output (e.g. which tests ran/passed before the hang) and can
    adapt — instead of a blind "timeout" with no signal."""
    import signal as _sig

    from aiforge_core.runtime import proc_signals
    proc_signals.kill_group(proc_signals.group_of(proc), _sig.SIGTERM)
    drained = _drain(proc)
    if drained is None:
        _kill_proc(proc)
        drained = ("", "")
    out, err = drained
    return {"ok": False, "timed_out": True, "code": None,
            "stdout": out[-_MAX_OBS:], "stderr": err[-_MAX_OBS:],
            "error": f"timed out after {timeout}s — PARTIAL output "
            "above. This is not a failure of your change: the command "
            "just ran longer than the limit. Next: run a NARROWER "
            "command (one test file or a single test case), or re-issue "
            "this exact command with a larger \"timeout\" (e.g. 600). Do "
            "NOT undo your edits over a timeout."}


def _await_exit(proc, timeout: int, sid) -> dict | None:
    """Poll until the process exits; a dict when it was stopped or timed out."""
    import time as _time
    from aiforge_core.runtime import chat_cancel
    deadline = _time.monotonic() + timeout
    while proc.poll() is None:
        if sid is not None and chat_cancel.is_cancelled(sid):
            _kill_proc(proc)
            return {"ok": False, "stopped": True, "error": "stopped by user"}
        if _time.monotonic() > deadline:
            return _timeout_result(proc, timeout)
        _time.sleep(0.2)
    return None


def _collect_output(proc) -> tuple[str, str]:
    """Bound communicate(): a daemon grandchild inheriting the stdout pipe
    (e.g. `npm run dev &`) keeps it open after the process exits, so an
    un-timed communicate() blocks forever even past the deadline."""
    try:
        ct = int(os.environ.get("AIFORGE_COMMUNICATE_TIMEOUT_S", "10"))
    except (TypeError, ValueError):
        ct = 10
    try:
        out, err = proc.communicate(timeout=ct)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_proc(proc)
        return _drain(proc) or ("", "")


def _t_run_command(args: dict, cwd: str) -> dict:
    cmd = args["cmd"]
    root = _workspace_root()
    base = str(root) if root is not None else cwd
    refusal = _run_refusal(cmd, args, base)
    if refusal is not None:
        return refusal
    # Default generous so dependency installs / builds (npm ci, mvn package,
    # pip install) aren't killed mid-run; agent may override per call.
    default_to = int(os.environ.get("AIFORGE_CHAT_CMD_TIMEOUT_S", "600"))
    timeout = int(args.get("timeout", default_to))
    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    try:
        # Its own process group, so the Stop button can kill the whole tree
        # (the shell + its children).
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
    stopped = _await_exit(proc, timeout, sid)
    if stopped is not None:
        return stopped
    out, err = _collect_output(proc)
    return {"ok": proc.returncode == 0, "code": proc.returncode,
            "stdout": out[-_MAX_OBS:], "stderr": err[-_MAX_OBS:]}


def _kill_proc(proc) -> None:
    from aiforge_core.runtime import proc_signals
    if not proc_signals.stop_group(proc_signals.group_of(proc),
                                   pid=getattr(proc, "pid", None),
                                   pause_s=0.0):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — already gone
            pass
    # Reap so the killed child's pipe FDs are freed (no zombie leak).
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


