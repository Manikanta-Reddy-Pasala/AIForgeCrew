"""Which paths of the user's REAL checkout a team-run shell command touches.

A team run works in its own worktree. Listing only the commands known to
write files missed the common ones: ``cd <repo> && prettier --write .``,
``ruff --fix``, ``make -C <repo>``, ``npm --prefix <repo> install``,
``python -c "open('<repo>/x', 'w')"``. So the rule is the other way round:
entering the repo, or naming a path in it, is refused unless the command
only READS (cat/grep/ls/diff…, a ``cp`` whose source is the repo, a
read-only git subcommand).
"""
from __future__ import annotations

import os
import re

from aiforge_core.runtime import shell_writes as sw

#: Commands that only read the paths they are given.
_READERS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg",
    "ag", "ls", "tree", "diff", "cmp", "wc", "stat", "file", "du", "md5sum",
    "shasum", "sha1sum", "sha256sum", "readlink", "realpath", "basename",
    "dirname", "test", "[", "echo", "printf", "true", "jq", "bat", "nl",
    "cut", "tr", "column", "od", "strings",
})
#: Readers only when no option (alone or in a cluster such as ``-Ei``) and no
#: script construct makes them write: sed ``-i`` / ``w file`` / ``e``,
#: awk ``-i inplace`` / ``> "f"`` / ``| "cmd"`` / ``system(``, sort ``-o``,
#: yq ``-i``.
_WRITE_FLAGS = {
    "sed": ("i", ("--in-place",)),
    "gsed": ("i", ("--in-place",)),
    "awk": ("i", ("--include",)),
    "gawk": ("i", ("--include",)),
    "sort": ("o", ("--output",)),
    "yq": ("i", ("--inplace",)),
}
_SCRIPT_WRITES = re.compile(
    r"[>|]|system\s*\(|(?:^|[;{}\s\d$])[we]\s|/[gpIiMm0-9]*[we](?:\s|$)")
_SCRIPTED = frozenset({"sed", "gsed", "awk", "gawk"})


def _writes_by_flag(head: str, args: list[str]) -> bool:
    letter, longs = _WRITE_FLAGS[head]
    for t in args:
        if t.startswith(longs):
            return True
        if t.startswith("-") and not t.startswith("--") and letter in t[1:]:
            return True
        if head in _SCRIPTED and not t.startswith("-") \
                and _SCRIPT_WRITES.search(t):
            return True
    return False


#: Readers with an optional OUTPUT operand (``uniq in out``, ``xxd in out``).
_ONE_OPERAND = frozenset({"uniq", "xxd"})
#: ``find`` reads unless one of these makes it act on what it finds.
_FIND_ACTS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                        "-fprint", "-fprintf", "-fls"})
#: Git subcommands that cannot change a repository.
_GIT_READS = frozenset({"status", "log", "diff", "show", "blame", "grep",
                        "ls-files", "ls-tree", "rev-parse", "describe",
                        "shortlog", "cat-file", "ls-remote", "rev-list"})
#: Git options whose next word is their value, not the subcommand.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree",
                             "--namespace", "--exec-path"})
_CHDIR = frozenset({"cd", "pushd"})
#: Absolute or home paths inside a word (``open('/repo/x','w')``).
_PATH_IN = re.compile(r"(?:(?<=[\s'\"=(,:])|^)(~?/[^\s'\"`;|&(),]+)")


def _expand(cmd: str) -> str:
    """``$REPO/x`` and ``${REPO}/x`` as the environment would expand them."""
    try:
        return os.path.expandvars(cmd)
    except Exception:  # noqa: BLE001
        return cmd


def _under(path: str, root: str) -> bool:
    from aiforge_core.runtime.team_run_life import fold
    fp = fold(path)
    return fp == root or fp.startswith(root.rstrip(os.sep) + os.sep)


def _named(tok: str, here: str) -> list[str]:
    """Every path a word names: the word itself, and absolute paths in it."""
    found = []
    whole = sw._resolve(tok, here)
    if whole and ("/" in tok or tok.startswith((".", "~"))):
        found.append(whole)
    for raw in _PATH_IN.findall(tok):
        p = sw._resolve(raw, here)
        if p and p not in found:
            found.append(p)
    return found


def _reads_only(rest: list[str], hit_idx: set) -> bool:
    head = os.path.basename(rest[0]) if rest else ""
    if head in _READERS:
        return True
    if head in _WRITE_FLAGS:
        return not _writes_by_flag(head, rest[1:])
    if head in _ONE_OPERAND:
        return (len([t for t in rest[1:] if not t.startswith("-")]) <= 1
                and "-r" not in rest)

    if head == "find":
        return not (_FIND_ACTS & set(rest[1:]))
    if head == "git":
        sub = next((t for i, t in enumerate(rest[1:], 1)
                    if not t.startswith("-")
                    and rest[i - 1] not in _GIT_VALUE_OPTS), "")
        return sub in _GIT_READS
    if head in ("cp", "rsync", "scp", "ln"):
        operands = [i for i, t in enumerate(rest[1:], 1) if not t.startswith("-")]
        # the repo may be a SOURCE, never the destination (the last operand)
        return bool(operands) and operands[-1] not in hit_idx
    return False


def touches(cmd: str, here: str, root: str) -> list[str]:
    """Paths under ``root`` (folded) that ``cmd``, run from ``here``, enters
    or names in a command that does not only read them."""
    hits: list[str] = []

    def add(p: str) -> None:
        if p not in hits:
            hits.append(p)

    for line in sw._strip_heredocs(_expand(cmd)):
        for seg in sw._segments(sw._tokens(line)):
            redirs, rest = sw._redirect_targets(seg)
            rest = sw._strip_prefixes(rest)
            for raw in redirs:
                p = sw._resolve(raw, here)
                if p and _under(p, root):
                    add(p)
            if not rest:
                continue
            head = os.path.basename(rest[0])
            if head in _CHDIR:
                target = sw._resolve(rest[1] if len(rest) > 1 else "~", here)
                if target and _under(target, root):
                    add(target)                 # nothing may run in there
                here = target or here
                continue
            if head in sw._SHELLS and "-c" in rest[1:-1]:
                for p in touches(rest[rest.index("-c") + 1], here, root):
                    add(p)
                continue
            hit_idx = {i for i, tok in enumerate(rest[1:], 1)
                       if any(_under(p, root) for p in _named(tok, here))}
            if hit_idx and not _reads_only(rest, hit_idx):
                for i in sorted(hit_idx):
                    for p in _named(rest[i], here):
                        if _under(p, root):
                            add(p)
    return hits


__all__ = ["touches"]
