"""A shell command that would throw away the run's own edits is refused.

click af9, 20 minutes in: ``rm -f … src/click/core.py src/click/types.py …``
then a restore from git — every edit of the run gone, "I need to implement
it from scratch". attrs af1 did the same with ``git checkout``. With
approvals off the delete guard confirms such a command by itself; that
guard protects the user's files from the agent, this one protects the
agent's work from a panicked reset.

Refused, when it would remove or overwrite a file a tool of THIS turn wrote
(``st.file_hashes``) that still exists: ``rm``/``unlink``/``git rm`` (globs
expanded), ``git checkout [<commit>] [--] <path>``, ``git restore`` (not
``--staged`` alone), ``git stash`` / ``stash push`` (not pop/apply/list…),
``git reset --hard``, ``git clean -f`` (not ``-n``/``-X``) — through ``cd``,
``git -C``, multi-line commands, subshells, ``bash -c`` and prefixes. A
throwaway named file by file (debug.py, repro_x.py, the chat's scratch
folder) may be removed: that is cleanup. ``> file``, ``cp``/``mv`` onto it and
``truncate`` count as overwriting it. The model can still undo: fix the
file with file_patch, editor ``undo_edit`` for the last edit, or send the
same command again with ``"discard": true``. Off switch:
``AIFORGE_CHAT_DISCARD_GUARD=0``.
"""
from __future__ import annotations

import glob
import os
import re

_STASH_KEEPS = {"list", "show", "pop", "apply", "drop", "clear", "branch", "create",
                "store"}
_GLOB = re.compile(r"[*?\[]")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_DISCARD_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _paths(words: list, here: str) -> list:
    """Absolute paths the words name, wildcards expanded."""
    out = []
    for w in words:
        full = os.path.join(here, os.path.expanduser(w))
        out += [os.path.realpath(g) for g in glob.glob(full)] if _GLOB.search(w) \
            else [os.path.realpath(full)]
    return out


def _operands(words: list, takes_value=()) -> list:
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w == "--":
            continue
        elif w.startswith("-"):
            skip = w in takes_value
        else:
            out.append(w)
    return out


class _Git:
    """Tracked files per repo, read once per check (one `git ls-files` per
    repo, not a process per file). Outside a git repo nothing is untracked:
    every edited file there is protected."""

    def __init__(self):
        self._top: dict = {}
        self._files: dict = {}

    def top(self, d: str) -> "str | None":
        if d not in self._top:
            import subprocess
            try:
                p = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                                   capture_output=True, timeout=10)
                self._top[d] = (os.path.realpath(p.stdout.decode().strip())
                                if p.returncode == 0 else None)
            except Exception:  # noqa: BLE001
                self._top[d] = None
        return self._top[d]

    def tracked(self, path: str) -> bool:
        d = path if os.path.isdir(path) else os.path.dirname(path)
        top = self.top(d or ".")
        if top is None:
            return True
        if top not in self._files:
            import subprocess
            try:
                p = subprocess.run(["git", "-C", top, "ls-files", "-z"],
                                   capture_output=True, timeout=20)
                self._files[top] = {os.path.realpath(os.path.join(top, f))
                                    for f in p.stdout.decode(errors="replace").split("\0") if f}
            except Exception:  # noqa: BLE001
                self._files[top] = None
        files = self._files[top]
        return True if files is None else os.path.realpath(path) in files


def _git_hits(args: list, here: str, cover, git: "_Git") -> list:
    """Edited files a git command would overwrite or throw away."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            here = os.path.realpath(os.path.join(here, args[i + 1]))
            i += 2
        elif args[i] in ("-c", "--git-dir", "--work-tree") and i + 1 < len(args):
            i += 2
        else:
            i += 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    after = rest[rest.index("--") + 1:] if "--" in rest else None
    repo = git.top(here) or here               # stash / reset act on the whole repo
    tracked = lambda ps: [p for p in ps if git.tracked(p)]  # noqa: E731
    forced = any(f in rest for f in ("-f", "--force", "--discard-changes"))
    if sub == "rm":
        return cover(_paths(_operands(rest), here))
    if sub in ("checkout", "switch"):
        if after is not None:
            return cover(_paths(after, here))
        if forced:
            return tracked(cover([repo]))        # a forced switch drops local changes
        if sub == "switch":
            return []
        ops = _operands(rest, ("-b", "-B", "--orphan"))
        if not ops or any(f in rest for f in ("-b", "-B", "--orphan")):
            return []
        if os.path.exists(os.path.join(here, ops[0])):
            return cover(_paths(ops, here))         # git checkout <paths>
        return cover(_paths(ops[1:], here))         # git checkout <commit> <paths>
    if sub == "restore":
        if ("--staged" in rest or "-S" in rest) and not (
                "--worktree" in rest or "-W" in rest):
            return []                               # only unstages
        return cover(_paths(after if after is not None else
                            _operands(rest, ("-s", "--source")), here))
    if sub == "stash":
        if rest and rest[0] in _STASH_KEEPS:
            return []
        got = cover(_paths(after, here) if after else [repo])
        untracked_too = any(f in rest for f in ("-u", "-a", "--include-untracked", "--all"))
        return got if untracked_too else tracked(got)
    if sub == "reset" and "--hard" in rest:
        return tracked(cover([repo]))               # untracked files stay
    if sub == "clean":
        flags = "".join(w[1:] for w in rest if w.startswith("-") and not w.startswith("--"))
        force = "f" in flags or "--force" in rest
        if not force or "n" in flags or "X" in flags or "--dry-run" in rest:
            return []
        ops = _operands(rest, ("-e", "--exclude"))
        return [p for p in cover(_paths(ops, here) if ops else [here]) if not git.tracked(p)]
    return []


#: A throwaway the run made and may delete by name: debug.py, repro_x.py,
#: tmp_check.py, scratch/… — never real work, whatever the request names.
_SCRATCH_NAME = re.compile(r"^(?:debug|repro|tmp|temp|scratch|probe|try|check|test_tmp)"
                           r"[\w.-]*$", re.I)


def _is_scratch(path: str) -> bool:
    from aiforge_core.runtime import git_target
    try:
        if git_target.is_scratch(path):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_SCRATCH_NAME.match(os.path.basename(path)))


_PROBE_TEST = re.compile(r"^(?:test_[\w-]+|[\w-]+_test)\.py$")


def _probe_file(path: str, git: "_Git", goal: str) -> bool:
    """A probe test the run wrote for itself: untracked, a test file's name,
    at the repo root (not in tests/), and not a name the request uses
    (as a word, with or without ``.py``). New modules under a package or
    src/ are real work and stay protected."""
    name = os.path.basename(path)
    if not _PROBE_TEST.match(name) or git.tracked(path):
        return False
    parent = os.path.dirname(os.path.realpath(path))
    if parent != git.top(parent):
        return False          # tests/ holds the tests the request asked for
    stem = name[:-3]
    return not re.search(rf"(?<![\w]){re.escape(stem)}(?![\w])", goal or "")


def hits(cmd: str, cwd: str, edited, git: "_Git | None" = None, goal: str = "") -> list:
    """The edited files ``cmd`` would remove or overwrite (paths as given in
    ``edited``). ``goal``: the request — a new file it does not name, removed
    by name, is the run's own scratch."""
    from aiforge_core.runtime import shell_writes as sw
    git = git or _Git()
    live = {os.path.realpath(p): p for p in (edited or ()) if os.path.exists(p)}
    if not live or not cmd:
        return []

    def cover(targets: list) -> list:
        out = []
        for t in targets:
            pre = t.rstrip("/") + "/"
            out += [v for k, v in live.items() if k == t or k.startswith(pre)]
        return out

    here = os.path.realpath(cwd or ".")
    found: list = []
    for line in sw._strip_heredocs((cmd or "").replace("\\\n", " ")):
        for seg in sw._segments(sw._tokens(line)):
            if seg[0] == "cd":
                to = seg[1] if len(seg) > 1 else "~"
                if not re.search(r"[$`]", to) and to != "-":
                    here = os.path.realpath(os.path.join(here, os.path.expanduser(to)))
                continue
            redirs, seg = sw._redirect_targets(seg)
            found += cover(_paths(redirs, here))            # `> src/core.py` overwrites
            toks = sw._strip_prefixes(seg)
            if not toks:
                continue
            head, args = os.path.basename(toks[0].lstrip("\\")), toks[1:]
            if head in sw._SHELLS:
                flag = next((i for i, a in enumerate(args)
                             if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a)), None)
                if flag is not None and flag + 1 < len(args):
                    found += hits(args[flag + 1], here, edited, git, goal)
            elif head in ("rm", "unlink"):
                ops = _operands(args)
                named = [o for o in ops if not _GLOB.search(o)]
                targets = _paths(ops, here)
                one_by_one = len(named) == len(ops) and not any(
                    os.path.isdir(t) for t in targets)
                got = cover(targets)
                # removing, by name, a throwaway (debug.py) or a new file the
                # request does not name (a probe test the run wrote) is
                # cleanup: click af11 was refused 61 times deleting its own
                # untracked test_completion_item.py
                found += [p for p in got if not (one_by_one and (
                    _is_scratch(p) or _probe_file(p, git, goal)))]
            elif head in sw._DEST_LAST or head == "truncate":
                found += cover(_paths(sw._destination(args) if head != "truncate"
                                      else _operands(args, ("-s", "--size")), here))
            elif head == "git":
                found += _git_hits(args, here, cover, git)
    return list(dict.fromkeys(found))


def refusal(st, name: str, args: dict, cwd) -> "dict | None":
    """The result to hand back instead of running ``args``, or None."""
    if not _on() or not isinstance(args, dict) \
            or str(args.get("discard")).strip().lower() in ("true", "1", "yes"):
        return None
    try:
        from aiforge_core.runtime import git_target
        from aiforge_core.runtime.shell_writes import SHELL_TOOLS, command_of
        if name not in SHELL_TOOLS:
            return None
        st.discard_calls = getattr(st, "discard_calls", 0) + 1
        # where run_command really runs (a workspace root, not the file cwd)
        base = git_target.command_base(cwd) or str(cwd or ".")
        cmd = command_of(args) or ""
        lost = hits(cmd, base, (getattr(st, "file_hashes", None) or {}).keys(),
                    goal=str(getattr(st, "goal", "") or ""))
    except Exception:  # noqa: BLE001 — the guard never blocks on its own error
        return None
    if not lost:
        return None
    # The same untracked files refused, sent again as the very next shell
    # command: the model means it (looping on refusals cost 61 calls in one
    # run). Tracked work is never let through this way — it needs the flag.
    n = getattr(st, "discard_calls", 0)
    key = (base, tuple(sorted(lost)))
    last = getattr(st, "discard_last", None)
    st.discard_last = (key, n)
    if last and last[0] == key and last[1] == n - 1 and all(not _Git().tracked(p) for p in lost):
        st.discard_last = None
        return None
    shown = [os.path.relpath(p, base) for p in lost[:8]]
    return {"ok": False, "error": "would_discard_own_edits", "files": shown,
            "hint": ("This command would throw away your own edits from this turn "
                     f"to: {', '.join(shown)}. Do not start over — if an edit broke "
                     "a file, fix that part with file_patch or multi_edit. Only if "
                     "you really mean to discard that work, send "
                     "the same command again with \"discard\": true.")}


__all__ = ["hits", "refusal"]
