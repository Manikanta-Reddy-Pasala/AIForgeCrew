"""Reading a shell command before it runs: blanket git staging, and whether
it starts a long-lived server (which would block run_command)."""
from __future__ import annotations

import os
import re

_BASH = '.bash'

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
