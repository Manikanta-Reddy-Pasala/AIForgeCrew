"""Paths a shell command would WRITE — so the chat workspace jail covers the
shell, not only the file tools.

The jail asked before `file_write` touched a folder outside the chat, and the
agent's way around a refusal was `run_command` with `cat > /elsewhere/x.py` or
`cp`/`mv`. This reads a command the way a reviewer would and lists the paths it
writes: redirections, `tee`, the destination of cp/mv/install/rsync/ln, the
operands of touch/mkdir/rm/rmdir/truncate/chmod/chown, `sed -i` files,
`dd of=`, and `git -C DIR` for a mutating git subcommand. `cd DIR` is followed,
so `cd /other && echo x > f` names /other/f.

It is a best-effort reading, not a sandbox: a script, `python -c`, or a variable
expansion it cannot see through is not listed. The temp dir and the device dir
are never reported — scratch files there are not the "edited a repo it was never given"
problem the jail exists for.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile

SHELL_TOOLS = frozenset({"run_command", "run_shell", "bash", "shell"})

_SEP_RE = re.compile(r"&&|\|\||[;|\n]")          # segments are stripped after
_REDIR_RE = re.compile(r"^[\d&]?>>?(?P<path>.*)$")
_DEST_LAST = frozenset({"cp", "mv", "install", "rsync", "ln", "scp"})
_OPERANDS = frozenset({"touch", "mkdir", "rm", "rmdir", "truncate", "unlink",
                       "shred"})
_GIT_READONLY = frozenset({"status", "log", "diff", "show", "blame", "grep",
                           "ls-files", "rev-parse", "describe", "shortlog",
                           "cat-file", "remote", "config", "branch", "tag",
                           "fetch", "ls-remote", "help", "version"})
# chmod/chown: the first operand is the mode/owner, not a path.
_SKIP_FIRST = frozenset({"chmod", "chown", "chgrp"})


def _temp_roots() -> tuple[str, ...]:
    """This interpreter's temp dir (TMPDIR, else the platform default), any
    TMPDIR/TEMP/TMP the environment names, and the device dir (os.devnull's
    parent) — scratch and sinks, never a project."""
    roots = {tempfile.gettempdir(), os.path.dirname(os.devnull),
             *(os.environ.get(v, "") for v in ("TMPDIR", "TEMP", "TMP"))}
    return tuple(os.path.realpath(r) for r in roots if r)


def _is_temp(path: str) -> bool:
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep)
               for r in _temp_roots())


def _resolve(raw: str, cwd: str) -> "str | None":
    if not raw or raw.startswith("-") or "$" in raw or "`" in raw:
        return None
    try:
        return os.path.realpath(os.path.join(cwd, os.path.expanduser(raw)))
    except Exception:  # noqa: BLE001
        return None


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:            # an unbalanced quote: fall back to whitespace
        return segment.split()


def _redirect_targets(toks: list[str]) -> "tuple[list[str], list[str]]":
    """(redirect targets, the tokens left once redirections are removed)."""
    out, rest = [], []
    it = iter(toks)
    for tok in it:
        m = _REDIR_RE.match(tok)
        if not m:
            rest.append(tok)
            continue
        path = m.group("path") or next(it, "")
        if path and not path.startswith("&"):      # `2>&1` duplicates, no file
            out.append(path)
    return out, rest


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _after_mode(args: list[str]) -> list[str]:
    return _operands(args)[1:]                     # chmod/chown: mode/owner first


def _destination(args: list[str]) -> list[str]:
    """cp/mv/install/rsync/ln/scp: `-t DIR`, else the last of 2+ operands."""
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            return [args[i + 1]]
        if a.startswith("--target-directory="):
            return [a.split("=", 1)[1]]
    ops = _operands(args)
    return ops[-1:] if len(ops) >= 2 else []


def _sed_files(args: list[str]) -> list[str]:
    """The files `sed -i` edits: its operands minus the script — the first
    operand, unless the script came via -e/-f (whose values are skipped)."""
    if not any(a.startswith(("-i", "--in-place")) for a in args):
        return []
    ops: list[str] = []
    scripted = False
    it = iter(args)
    for a in it:
        if a in ("-e", "--expression", "-f", "--file"):
            scripted = True
            next(it, None)
        elif a.startswith(("--expression=", "--file=")):
            scripted = True
        elif not a.startswith("-"):
            ops.append(a)
    return ops if scripted else ops[1:]


def _dd_output(args: list[str]) -> list[str]:
    return [a[3:] for a in args if a.startswith("of=")]


def _git_dir(args: list[str]) -> list[str]:
    """`git -C DIR <mutating subcommand>` writes DIR."""
    if "-C" not in args:
        return []
    i = args.index("-C")
    if i + 1 >= len(args):
        return []
    sub = next((a for a in args[i + 2:] if not a.startswith("-")), "")
    return [args[i + 1]] if sub and sub not in _GIT_READONLY else []


_HANDLERS = {"tee": _operands, "sed": _sed_files, "dd": _dd_output, "git": _git_dir,
             **dict.fromkeys(_DEST_LAST, _destination),
             **dict.fromkeys(_OPERANDS, _operands),
             **dict.fromkeys(_SKIP_FIRST, _after_mode)}
_PREFIXES = frozenset({"sudo", "env", "nohup", "time", "command"})


def _command_targets(toks: list[str]) -> list[str]:
    while toks and (toks[0] in _PREFIXES
                    or ("=" in toks[0] and not toks[0].startswith("-"))):
        toks = toks[1:]                # sudo/env prefixes and VAR=value
    if not toks:
        return []
    handler = _HANDLERS.get(os.path.basename(toks[0]))
    return handler(toks[1:]) if handler else []


def _segment_writes(toks: list[str], here: str) -> list[str]:
    """Absolute, non-temp paths one command segment writes."""
    redirs, rest = _redirect_targets(toks)
    paths = (_resolve(raw, here) for raw in redirs + _command_targets(rest))
    return [p for p in paths if p and not _is_temp(p)]


def shell_write_targets(cmd: str, cwd: str) -> list[str]:
    """Absolute paths ``cmd`` would write, outside temp dirs, in order."""
    here = os.path.realpath(cwd or os.getcwd())
    found: list[str] = []
    for segment in _SEP_RE.split(cmd or ""):
        toks = _tokens(segment.strip())
        if toks and toks[0] == "cd":
            here = _resolve(toks[1] if len(toks) > 1 else "~", here) or here
            continue
        found.extend(p for p in dict.fromkeys(_segment_writes(toks, here))
                     if p not in found)
    return found


def command_of(args: dict) -> str:
    """The command string of a shell tool call."""
    return str((args or {}).get("cmd") or (args or {}).get("command") or "")


__all__ = ["SHELL_TOOLS", "shell_write_targets", "command_of"]
