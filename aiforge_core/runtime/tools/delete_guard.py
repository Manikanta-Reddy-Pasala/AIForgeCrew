"""Shared destructive-delete detection.

Policy: agents act autonomously for everything that unblocks the job
(install, build, run, edit, move, commit) — EXCEPT deleting files / data,
which must be confirmed by the user first. This module centralises the
pattern match used by both the conversational chat agent (``run_command``)
and the team Doer (``bash``). Override the policy per-process with
``AIFORGE_ALLOW_DELETE=1`` (or the chat-specific ``AIFORGE_CHAT_ALLOW_DELETE``).

Inside the docker-mode sandbox the agent owns the box and the folders the user
mounted into it, so a LOCAL file delete runs free there. Still confirmed even in
the box: deleting AIForge's own data (its config folder — settings,
credentials, memory — outside ``repos/`` and the chat workspaces), and deletes
that reach past the box (a database table, a Kubernetes object) or destroy a
device.
"""
from __future__ import annotations

import os
import re

_DELETE_PATTERNS = [
    # BOUNDED, and the inner class requires a letter. `(-[a-z]*\s+)*` let an
    # option match as just "-" plus spaces, so a long run of dashes and spaces
    # could be split many ways before the match failed. Nobody passes rm more
    # than a few option groups.
    r"\brm\s+(?:-[a-z]+\s+){0,5}",   # rm, rm -rf, rm -f …
    r"\brmdir\b", r"\bunlink\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+branch\s+-D\b",
    r"\bfind\b.*-delete\b",
    r"\bdrop\s+(table|database)\b",
    r"\btruncate\s+table\b",
    r"\btruncate\s+-",                # coreutils: truncate -s 0 file
    # `dd ... of=<file>` overwrites/truncates the target (raw disk OR a file);
    # of=/dev/null is a harmless sink so exclude it.
    r"\bdd\b.*\bof=(?!/dev/null\b)",
    r"\bcp\s+/dev/null\s",           # cp /dev/null file → truncates file
    r">\s*/dev/sd",                   # writing over a raw disk
    # NOTE: plain `>` output redirection (`cat x > out.txt`, `npm build > log`)
    # is creation/overwrite, NOT a delete — and the autonomous Doer has no
    # approval path, so flagging it would hard-fail every redirect. Not matched.
    r"\bmkfs\b", r"\bshred\b",
    r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune)\b",
    r"\bkubectl\s+delete\b",
]
_COMPILED = [re.compile(p) for p in _DELETE_PATTERNS]
# The LOCAL file deletes — free inside the sandbox (the rest still ask there).
_LOCAL_DELETES = frozenset({
    r"\brm\s+(?:-[a-z]+\s+){0,5}", r"\brmdir\b", r"\bunlink\b",
    r"\bgit\s+clean\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+branch\s+-D\b",
    r"\bfind\b.*-delete\b", r"\btruncate\s+-", r"\bdd\b.*\bof=(?!/dev/null\b)",
    r"\bcp\s+/dev/null\s",
})
# Sub-folders of the AIForge config folder that are the agent's working area,
# not AIForge's own data.
_WORK_AREAS = frozenset({"repos", "chat-workspaces", "workspaces", "tmp"})


def _in_sandbox() -> bool:
    return os.environ.get("AIFORGE_SANDBOX", "").strip().lower() in (
        "1", "true", "yes", "on")


_CFG_DIRNAME = ".aiforge"


def _config_forms() -> list[str]:
    home = os.environ.get("HOME", "")
    default = os.path.join(home, _CFG_DIRNAME)
    cfg = os.environ.get("AIFORGE_CONFIG_DIR") or default
    forms = {cfg.rstrip("/")}
    # The same folder written the ways a command can spell it — a literal ~ and
    # the two $HOME forms the shell would expand before the guard ever sees them.
    forms.update(f"{p}/{_CFG_DIRNAME}" for p in ("~", "$HOME", "${HOME}"))
    if home:
        forms.add(default)
    return [f for f in forms if f]


def touches_aiforge_data(cmd: str) -> bool:
    """Whether ``cmd`` names AIForge's own data — the config folder itself or
    anything in it outside the working areas (repos/, chat workspaces)."""
    for form in _config_forms():
        for m in re.finditer(re.escape(form) + r"(/[^\s'\";|&)]*)?", cmd or ""):
            first = (m.group(1) or "").strip("/").split("/")[0]
            if first not in _WORK_AREAS:
                return True
    return False

REFUSAL = (
    "Refused: this command deletes files/data. The agent does every other "
    "operation autonomously but must ASK before deleting. Stop and ask the "
    "user to confirm this exact command; only re-run it after they agree."
)


def _protected(path: str) -> bool:
    """AIForge's own data: the config folder, anything in it outside the
    working areas, or any folder that CONTAINS it (`rm -rf ~`)."""
    cfg = os.path.realpath(os.environ.get("AIFORGE_CONFIG_DIR")
                           or os.path.join(os.environ.get("HOME", "/"), ".aiforge"))
    p = os.path.realpath(path)
    if p == cfg or cfg.startswith(p.rstrip(os.sep) + os.sep):
        return True
    if p.startswith(cfg + os.sep):
        return os.path.relpath(p, cfg).split(os.sep)[0] not in _WORK_AREAS
    return False


def _deletes_aiforge_data(cmd: str, cwd: "str | None") -> bool:
    """Whether any path ``cmd`` deletes resolves into AIForge's own data —
    following `cd`, `~` and relative paths (the shell-write reader), and the
    working folder itself for the delete forms that act on it implicitly
    (`git clean`, `git reset --hard`, `find . -delete`, `rm *`)."""
    here = cwd or os.getcwd()
    if touches_aiforge_data(cmd):
        return True
    try:
        from aiforge_core.runtime.shell_writes import shell_write_targets
        targets = shell_write_targets(cmd, here)
    except Exception:  # noqa: BLE001 — unreadable: the textual check stands
        targets = []
    if re.search(r"\bgit\s+(clean|reset)\b|\bfind\b", cmd or ""):
        targets.append(here)
    return any(_protected(t) for t in targets)


def is_destructive_delete(cmd: str, cwd: "str | None" = None) -> bool:
    if not cmd:
        return False
    low = cmd.lower()
    hits = [p.pattern for p in _COMPILED if p.search(low)]
    if not hits:
        return False
    if (_in_sandbox() and all(h in _LOCAL_DELETES for h in hits)
            and not _deletes_aiforge_data(cmd, cwd)):
        return False            # the box and its mounted folders are the agent's
    return True


# ── deleting a repository's version history ─────────────────────────────────
# `rm -rf .git` (or .hg/.svn/.jj/.bzr/_darcs) throws away every commit, branch
# and stash, and no edit can bring it back. It is never a step towards a coding
# goal: an agent reaches for it to "start clean", to dodge a git refusal, or to
# re-fetch the project from upstream over its own work. So it is a floor of its
# own: approvals being off, the sandbox, or an allow-delete opt-in do not run it
# — only a human who sees this exact command can.
_VCS_DIRS = (".git", ".hg", ".svn", ".jj", ".bzr", "_darcs")
_VCS_ALT = "|".join(re.escape(d) for d in _VCS_DIRS)
# A path naming the metadata folder itself: `.git`, `./.git/`, `repo/.git`,
# `"/abs/.git"`, `"$(git rev-parse --show-toplevel)/.git"` — never a sibling
# name (`.gitignore`, `.github`, `.svnignore`).
_VCS_PATH = re.compile(r"^(?:.*[/\\])?(?:" + _VCS_ALT + r")[/\\]?$")
# Emptying it is the same loss: `.git/*`, `.git/objects`, `.git/refs/...`.
# Routine files inside it (`index.lock`, `MERGE_HEAD`, hooks) stay free.
_VCS_GUTS = re.compile(r"^(?:.*[/\\])?(?:" + _VCS_ALT
                       + r")[/\\](?:\*|objects|refs|store|data|db|wc\.db"
                       r"|logs)(?:[/\\].*)?$")
# Globs that also expand to the metadata folder: `.*`, `.??*`, `.[!.]*`,
# `.g*`, `.git*`.
_VCS_GLOB = re.compile(r"^(?:.*[/\\])?(?:\.\*|\.\?+\*|\.\[[^\]]*\]\*|\.[a-z]{1,3}\*"
                       r"|(?:" + _VCS_ALT + r")\*)$")
_GIT_DIR_SUBST = re.compile(r"rev-parse\s+--(?:absolute-)?git-(?:common-)?dir")
# Shell verbs that remove (or move away) what they are given.
_REMOVE_VERBS = frozenset({"rm", "rmdir", "rd", "del", "erase", "unlink",
                           "remove-item", "ri", "rimraf", "trash", "trash-put",
                           "gio", "mv", "move", "move-item", "shred", "srm"})
# Words that come before the real verb: shell keywords, runners, privilege.
_WRAPPERS = frozenset({"sudo", "doas", "command", "builtin", "env", "nice",
                       "ionice", "nohup", "time", "timeout", "xargs", "busybox",
                       "exec", "npx", "pnpx", "bunx", "call", "then", "do",
                       "else", "elif", "{", "!", "if", "while", "until"})
# Wrapper options that take a value (`sudo -u bob`, `nice -n 10`).
_VALUE_OPTS = frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-U", "-n", "-c",
                         "-s", "-k", "-S", "-I", "-L", "-P", "-d", "-a", "-E"})
# Interpreters whose inline code can delete: only then is code text read.
_INTERP = re.compile(r"(?:^|[\s;&|(])(?:python[0-9.]*|py|node|deno|bun|ruby|"
                     r"perl|php|pwsh|powershell|yaegi|gorun|go\s+run|cargo|"
                     r"rust-script|dotnet|dotnet-script|scala|kotlinc?|groovy|"
                     r"jshell|ts-node|tsx|irb)\b")
# In-language removals through a filesystem API.
_CODE_REMOVE = re.compile(
    r"(?:\bshutil\.rmtree|\bos\.(?:remove|unlink|rmdir|removedirs|RemoveAll)"
    r"|\bfs(?:\.promises)?\.(?:rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync)"
    r"|\bFileUtils\.(?:rm_rf|rm_r|rm|remove_dir|remove_entry(?:_secure)?)"
    r"|\bDirectory\]?(?:\.|::)Delete|\bremove_dir_all|\brimraf(?:\.sync)?"
    r"|\.(?:rmSync|rmdirSync)|\brequire\(\s*['\"](?:node:)?fs(?:/promises)?['\"]\s*\)\.(?:rm|rmdir)"
    r"|\bFile\.delete|\bFiles\.delete(?:IfExists)?|\bdeleteDirectory"
    r"|\bRemove-Item)\s*\(?\s*[^)\n]*?['\"](?:[^'\"]*[/\\])?(?:" + _VCS_ALT
    + r")[/\\]?['\"]")
# find . -name .git ... -delete / -exec rm   (not `-prune`, not `.gitkeep`)
_FIND_VCS = re.compile(r"-(?:i?name|path)\s+['\"]?(?:\*/)?(?:" + _VCS_ALT
                       + r")['\"]?(?=\s|$)")
# git commands that destroy history beyond the reflog.
_GIT_PURGE = re.compile(
    r"\bgit\b[^\n;&|]*\breflog\s+expire\b[^\n;&|]*--expire(?:-unreachable)?="
    r"(?:now|all|0)\b")
_TMP_ROOTS = ("/tmp/", "/var/tmp/", "/private/tmp/", "/var/folders/",
              "/dev/shm/")


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc BODIES: text written into a file (a Dockerfile, a script)
    is not run by this command."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if m:
            end = m.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                i += 1
        i += 1
    return "\n".join(out)


def _split_segments(cmd: str) -> list[str]:
    """Split on ; && || | & newline ( ) and backticks, but only OUTSIDE quotes
    — `echo "a && rm -rf .git"` is one echo. `$( … )` stays inside its word."""
    segs, buf, quote, depth, i = [], [], "", 0, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 2; continue
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c; buf.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            buf.append(c); buf.append(cmd[i + 1]); i += 2; continue
        elif cmd.startswith("$(", i):
            depth += 1; buf.append("$("); i += 2; continue
        elif c == ")" and depth:
            depth -= 1; buf.append(c)
        elif depth == 0 and (c in ";|&\n`()"):
            segs.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [x for x in segs if x.strip()]


def _words(segment: str) -> list[str]:
    import shlex
    try:
        return shlex.split(segment, posix=True)
    except ValueError:           # unbalanced quote: fall back to plain words
        return segment.split()


def _verb_and_args(words: list[str]) -> tuple[str, list[str]]:
    """Skip `sudo -u bob`, `env X=1`, `timeout 60`, `then`, `!` … to the verb."""
    while words:
        w = words[0]
        low = os.path.basename(w).lower()
        if low in _WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            words = words[1:]
            # the wrapper's own options, and a value after options that take one
            while words and words[0].startswith("-") and len(words) > 1:
                opt = words[0]
                words = words[1:]
                if opt in _VALUE_OPTS and words and not words[0].startswith("-"):
                    words = words[1:]
            if low in ("timeout", "nice") and words and re.match(
                    r"^[0-9][0-9.]*[smhd]?$", words[0]):
                words = words[1:]
            continue
        break
    if not words:
        return "", []
    verb = os.path.basename(words[0]).lower()
    if verb.endswith(".exe"):
        verb = verb[:-4]
    return verb, [w.strip() for w in words[1:]]


def _is_option(a: str) -> bool:
    return a.startswith("-") or (a.startswith("/") and len(a) <= 3
                                 and a[1:].isalpha())


def _names_vcs(a: str) -> bool:
    return bool(_VCS_PATH.match(a) or _VCS_GUTS.match(a) or _VCS_GLOB.match(a)
                or _GIT_DIR_SUBST.search(a))


def _parent(path: str) -> str:
    p = re.sub(r"[/\\]+$", "", path)
    return os.path.normpath(os.path.dirname(p) or ".")


def _is_repo_root(path: str, base: "str | None") -> bool:
    """Whether `path` (a folder an rm removes) holds a repository's history —
    `rm -rf proj` after `cd ..` loses it as surely as `rm -rf proj/.git`.
    Scratch repos under a temp folder are the agent's own and stay free."""
    if base is None or not path or "*" in path or "$" in path:
        return False
    full = os.path.normpath(os.path.join(base, os.path.expanduser(path)))
    if any((full + "/").startswith(t) for t in _TMP_ROOTS) or full in ("/", ""):
        return False
    return any(os.path.isdir(os.path.join(full, d)) for d in _VCS_DIRS)


def _copy_dest(verb: str, args: list[str], base: "str | None") -> list[str]:
    """Folders a copy/clone in this same command creates (never `.` or the
    folder the command runs in): dropping the history of such a scratch copy
    loses nothing."""
    ops = [a for a in args if not _is_option(a)]
    if verb == "git":
        if not ops or ops[0] not in ("clone", "worktree"):
            return []
        ops = ops[1:]
        if ops and ops[0] == "add":                 # git worktree add <path>
            return [ops[1]] if len(ops) > 1 else []
        urls = [o for o in ops if re.search(r"://|^git@|\.git$", o)]
        after = ops[ops.index(urls[0]) + 1:] if urls else ops[1:]
        if after:                                   # git clone [opts] URL DIR
            return [after[-1]]
        src = urls[0] if urls else (ops[0] if ops else "")
        name = re.sub(r"\.git$", "", src.rstrip("/").split("/")[-1])
        return [name] if name else []
    if len(ops) < 2:
        return []
    dest = ops[-1]
    out = [dest]
    if dest.endswith(("/", "\\")):                  # cp -r src dir/ -> dir/src
        out += [os.path.join(dest, os.path.basename(s.rstrip("/\\")))
                for s in ops[:-1]]
    return out


_COPY_VERBS = frozenset({"cp", "rsync", "robocopy", "xcopy", "copy-item",
                         "ditto", "git", "degit"})


def removes_vcs_history(cmd: str, cwd: "str | None" = None) -> bool:
    """Whether ``cmd`` deletes, empties or moves away a repository's
    version-control folder (`.git`, `.hg`, `.svn`, `.jj`, `.bzr`, `_darcs`) or
    a whole repository, in any shell (POSIX, PowerShell, cmd.exe, through
    `bash -c` / `cmd /c`) or as inline code (Python, Node, Ruby, Go, Rust,
    .NET, Java), or purges the reflog. Routine files inside it (a stale
    `index.lock`), siblings (`.gitignore`, `.github/`), text written by a
    heredoc, and the history of a scratch copy made earlier in the same
    command do not count. ``cwd``, when given, also catches `rm -rf <repo>`."""
    if not cmd:
        return False
    if _GIT_PURGE.search(cmd):
        return True
    if _INTERP.search(cmd) and _CODE_REMOVE.search(cmd):
        return True
    body = _strip_heredocs(cmd)
    segs = _split_segments(body)
    base = cwd
    copies: set[str] = set()
    prev_names_vcs = False
    for seg in segs:
        words = _words(seg)
        verb, args = _verb_and_args(words)
        if not verb:
            prev_names_vcs = False
            continue
        # a nested shell: judge its script the same way
        if verb in ("bash", "sh", "zsh", "dash", "ksh", "fish") and "-c" in args:
            inner = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            if removes_vcs_history(inner, base):
                return True
        if verb == "cmd" and any(a.lower() in ("/c", "/k") for a in args):
            k = next(i for i, a in enumerate(args) if a.lower() in ("/c", "/k"))
            if removes_vcs_history(" ".join(args[k + 1:]), base):
                return True
        if verb in ("docker", "podman", "kubectl", "ssh"):
            prev_names_vcs = False
            continue             # runs elsewhere, not on this repository
        if verb == "cd" and args and base is not None:
            base = os.path.normpath(os.path.join(base, os.path.expanduser(args[0])))
            prev_names_vcs = False
            continue
        if verb == "find":
            joined = " ".join(args)
            m = _FIND_VCS.search(joined)
            if m and "-prune" not in joined[m.end():m.end() + 12]:
                if re.search(r"-delete\b|-exec(?:dir)?\s+(?:rm|rmdir|mv)\b", joined):
                    return True
                prev_names_vcs = True
                continue
        if verb in ("get-childitem", "gci", "ls", "dir", "echo", "printf") \
                and any(_VCS_PATH.match(a) for a in args):
            prev_names_vcs = True
            continue
        if verb in _COPY_VERBS:
            copies.update(os.path.normpath(d.rstrip("/\\"))
                          for d in _copy_dest(verb, args, base)
                          if d.rstrip("/\\") not in ("", ".", "./"))
            prev_names_vcs = False
            continue
        if verb in _REMOVE_VERBS:
            if verb == "gio" and (not args or args[0] not in ("trash", "remove")):
                prev_names_vcs = False
                continue
            ops = [a for a in args if not _is_option(a)]
            if verb in ("mv", "move", "move-item"):
                if "-t" in args or any(a.startswith("--target-directory") for a in args):
                    t = args.index("-t") + 1 if "-t" in args else -1
                    ops = [a for a in ops if t < 0 or a != args[t]]
                else:
                    ops = ops[:-1]          # the last operand is the destination
            if not ops and prev_names_vcs:  # find … .git | xargs rm -rf
                return True
            if _GIT_DIR_SUBST.search(seg):  # rm -rf $(git rev-parse --git-dir)
                return True
            for a in ops:
                if _names_vcs(a) and _parent(a) not in copies:
                    return True
                if verb not in ("mv", "move", "move-item", "unlink") \
                        and os.path.normpath(a.rstrip("/\\")) not in copies \
                        and _is_repo_root(a, base):
                    return True
        prev_names_vcs = False
    return False


def code_removes_vcs_history(code: str) -> bool:
    """Whether notebook/script CODE deletes a version-control folder through a
    filesystem API (`shutil.rmtree('.git')`, `fs.rmSync('.git')` …)."""
    return bool(code and _CODE_REMOVE.search(code))


# Set by the chat approval gate ONLY after a human approved this exact call
# (and stripped from whatever the model sent), so the shell's own floor can
# tell a seen-and-approved history delete from one the model "confirmed".
VCS_APPROVED_KEY = "_vcs_history_approved"

VCS_HISTORY_REFUSAL = (
    "Refused: this command deletes the repository's version history (the "
    "{dir} folder) — every commit, branch and stash, with no way back. That is "
    "never needed to finish a coding task, and it is not run even with "
    "approvals off. Keep the existing repository: to see the original code use "
    "`git show HEAD:<path>` or `git diff`; to undo one file use "
    "`git checkout -- <path>`. If the user really wants the history gone, ask "
    "them to do it themselves."
)


def vcs_history_refusal(cmd: str) -> str:
    low = (cmd or "").lower()
    found = next((d for d in _VCS_DIRS if d in low), ".git")
    return VCS_HISTORY_REFUSAL.format(dir=found)


def allow_delete(env_keys: tuple[str, ...] = ("AIFORGE_ALLOW_DELETE",)) -> bool:
    for k in env_keys:
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False
