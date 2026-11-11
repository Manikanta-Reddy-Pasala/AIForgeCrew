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
# `"/abs/.git"` — never something inside it (`.git/index.lock` is routine) and
# never a sibling name (`.gitignore`, `.github`, `.svnignore`).
_VCS_PATH = re.compile(r"""^(?:.*[/\\])?(?:""" + _VCS_ALT + r""")[/\\]?$""")
# Globs that also expand to the metadata folder: `.*`, `.[!.]*`, `.git*`.
_VCS_GLOB = re.compile(r"""^(?:.*[/\\])?(?:\.\*|\.\[!?\^?\.\]\*|(?:""" + _VCS_ALT
                       + r""")\*)$""")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n]|\$\(|`|\(|\)")
# Shell verbs that remove (or move away) what they are given.
_REMOVE_VERBS = frozenset({"rm", "rmdir", "rd", "del", "erase", "unlink",
                           "remove-item", "ri", "rimraf", "trash", "mv",
                           "move", "move-item", "shred", "srm"})
_WRAPPERS = frozenset({"sudo", "doas", "command", "builtin", "env", "nice",
                       "nohup", "time", "xargs", "busybox", "exec", "npx",
                       "pnpx", "bunx", "yarn", "pnpm", "call", "cmd", "/c"})
# In-language removals: shutil.rmtree('.git'), fs.rmSync(".git"), os.RemoveAll,
# FileUtils.rm_rf, Directory.Delete, std::fs::remove_dir_all, unlink(...).
_CODE_REMOVE = re.compile(
    r"""(?:rmtree|rmSync|rmdirSync|rm_rf|rm_r|remove_dir_all|RemoveAll|"""
    r"""deleteDirectory|Delete|rmdir|remove|unlink|rm)\s*\(\s*[^)]*?['"]"""
    r"""(?:[^'"]*[/\\])?(?:""" + _VCS_ALT + r""")[/\\]?['"]""")
# find . -name .git -exec rm -rf {} + / -delete
_FIND_REMOVE = re.compile(
    r"""\bfind\b[^;&|\n]*-(?:i?name|path)\s+['"]?(?:\*/)?(?:""" + _VCS_ALT
    + r""")['"]?[^;&|\n]*(?:-delete\b|-exec\s+(?:rm|rmdir)\b)""")


def _words(segment: str) -> list[str]:
    import shlex
    try:
        return shlex.split(segment, posix=True)
    except ValueError:           # unbalanced quote: fall back to plain words
        return segment.split()


def _segment_removes_vcs(segment: str, copies: frozenset = frozenset()) -> bool:
    words = _words(segment)
    # Skip `sudo`, `env X=1`, `time` … and their own options to reach the verb.
    while words and (words[0].lower() in _WRAPPERS
                     or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])
                     or (words[0].startswith("-") and len(words) > 1)):
        words = words[1:]
    if not words:
        return False
    verb = os.path.basename(words[0]).lower()
    if verb.endswith(".exe"):
        verb = verb[:-4]
    if verb not in _REMOVE_VERBS:
        return False
    args = [w.strip() for w in words[1:]]
    if verb == "mv" or verb in ("move", "move-item"):
        # Moving the history folder away loses it from the repo just the same;
        # only the SOURCE operands count (the last one is the destination).
        args = [a for a in args if not a.startswith("-")][:-1]
    for a in args:
        if a.startswith("-") or (a.startswith("/") and len(a) <= 3
                                 and a[1:].isalpha()):
            continue             # an option (`-rf`, Windows `/s`, `/q`)
        if (_VCS_PATH.match(a) or _VCS_GLOB.match(a)) \
                and _parent(a) not in copies:
            return True
    return False


_COPY_VERBS = frozenset({"cp", "rsync", "robocopy", "xcopy", "copy-item",
                         "ditto"})


def _fresh_copies(cmd: str) -> set[str]:
    """Folders this same command creates as a COPY (`cp -r a b`, `rsync a/ b`):
    dropping the history of a scratch copy just made loses nothing."""
    out: set[str] = set()
    for seg in _SEGMENT_SPLIT.split(cmd):
        words = _words(seg)
        if not words or os.path.basename(words[0]).lower() not in _COPY_VERBS:
            continue
        operands = [w for w in words[1:] if not w.startswith("-")]
        if len(operands) >= 2:
            out.add(os.path.normpath(operands[-1].rstrip("/\\")))
    return out


def _parent(path: str) -> str:
    p = re.sub(r"[/\\]+$", "", path)
    return os.path.normpath(os.path.dirname(p) or ".")


def removes_vcs_history(cmd: str) -> bool:
    """Whether ``cmd`` deletes (or moves away) a repository's version-control
    folder — `.git`, `.hg`, `.svn`, `.jj`, `.bzr`, `_darcs` — in any shell
    (POSIX, PowerShell, cmd.exe) or as an inline script (Python, Node, Ruby,
    Go, Rust, .NET). Deleting something INSIDE it (a stale `index.lock`) and
    sibling files (`.gitignore`, `.github/`) do not count."""
    if not cmd:
        return False
    if _CODE_REMOVE.search(cmd) or _FIND_REMOVE.search(cmd):
        return True
    copies = frozenset(_fresh_copies(cmd))
    return any(_segment_removes_vcs(seg, copies)
               for seg in _SEGMENT_SPLIT.split(cmd) if seg.strip())


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
