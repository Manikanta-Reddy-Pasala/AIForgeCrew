"""Paths a shell command would WRITE — so the chat workspace jail covers the
shell, not only the file tools.

The jail asked before `file_write` touched a folder outside the chat, and the
agent's way around a refusal was a shell tool with `cat > /elsewhere/x.py` or
`cp`/`mv`. This reads a command the way a reviewer would and lists the paths it
writes: redirections (`>`, `>>`, `>|`, `&>`, `>&file`, glued or spaced), `tee`,
the destination of cp/mv/install/rsync/ln/scp, the operands of
touch/mkdir/rm/rmdir/truncate/chmod/chown, `sed -i` files, `dd of=`,
`git clone … DIR` and `git -C DIR <mutating>`, `curl -o`, `wget -O/-P`,
`tar -C`, `unzip -d`. `cd DIR` is followed; `sudo`/`env`/`timeout`/`xargs`/
`nice` prefixes (and their options) are looked through; `bash -c '…'` is read
recursively; loop and if bodies (`do …`, `then …`) are commands; heredoc bodies
are data, not commands.

It is a best-effort reading, not a sandbox: a script, `python -c`, or a variable
expansion it cannot see through is not listed. The temp dirs and the device dir
are never reported — scratch files there are not the "edited a repo it was
never given" problem the jail exists for.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile

from aiforge_core.runtime.tools.tool_policy import _CMD_ARG_KEYS, _CMD_TOOLS

# Every tool that runs its argument through a shell — the same set the command
# risk gate reads, so `serve`/`watch_until`/`ui_check` are not a way around it.
SHELL_TOOLS = frozenset(_CMD_TOOLS)

_SEPARATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "{", "}"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})
_KEYWORDS = frozenset({"do", "then", "else", "elif", "!", "done", "fi", "esac"})
_HEADERS = frozenset({"for", "case", "select", "function"})
_CONDITIONS = frozenset({"while", "until", "if"})
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?")
_DEST_LAST = frozenset({"cp", "mv", "rsync", "ln", "scp"})
_OPERANDS = frozenset({"touch", "mkdir", "rm", "rmdir", "truncate", "unlink",
                       "shred"})
_GIT_READONLY = frozenset({"status", "log", "diff", "show", "blame", "grep",
                           "ls-files", "rev-parse", "describe", "shortlog",
                           "cat-file", "remote", "config", "branch", "tag",
                           "fetch", "ls-remote", "help", "version"})
# Look-through prefixes, and which of their options take a value.
_PREFIX_OPT_ARGS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
    "env": {"-u", "-C", "-S"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-n", "-P", "-L", "-d", "-a", "-E", "-s"},
    "nice": {"-n"},
    "nohup": set(), "time": set(), "command": set(), "exec": set(),
}
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})


def _temp_roots() -> tuple[str, ...]:
    """This interpreter's temp dir, the conventional ones (``/tmp`` resolves to
    ``/private/tmp`` on macOS, where gettempdir() is a /var/folders path), any
    TMPDIR/TEMP/TMP the environment names, and the device dir."""
    conventional = (os.path.join(os.sep, "tmp"), os.path.join(os.sep, "var", "tmp"))
    roots = {tempfile.gettempdir(), os.path.dirname(os.devnull), *conventional,
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


def _strip_heredocs(cmd: str) -> list[str]:
    """The command's lines with every heredoc BODY removed (it is data: a
    Dockerfile's `RUN mkdir -p /app` is not this shell writing /app)."""
    out: list[str] = []
    ending: list[str] = []
    for line in (cmd or "").splitlines():
        if ending:
            if line.strip() == ending[0]:
                ending.pop(0)
            continue
        out.append(line)
        ending = _HEREDOC_RE.findall(line)
    return out


def _tokens(line: str) -> list[str]:
    """Shell words with operators (`>`, `|`, `&&`, `;` …) as their own tokens —
    quotes respected, so `echo "a>b"` is not a redirect."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:            # an unbalanced quote: fall back to whitespace
        return line.split()


def _segments(toks: list[str]) -> list[list[str]]:
    """Split at command separators and drop control keywords, so the body of
    `for …; do cp a /p; done` or `if …; then …` is read as a command."""
    segs: list[list[str]] = [[]]
    for tok in toks:
        if tok in _SEPARATORS:
            segs.append([])
        else:
            segs[-1].append(tok)
    out = []
    for seg in segs:
        while seg and seg[0] in _KEYWORDS:
            seg = seg[1:]
        if not seg or seg[0] in _HEADERS:
            continue
        if seg[0] in _CONDITIONS:
            seg = seg[1:]              # the condition is a command too
        if seg:
            out.append(seg)
    return out


def _redirect_targets(toks: list[str]) -> "tuple[list[str], list[str]]":
    """(redirect targets, the tokens left once redirections are removed)."""
    out, rest = [], []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok not in _REDIRECTS or i + 1 >= len(toks):
            rest.append(tok)
            i += 1
            continue
        target = toks[i + 1]
        # `>&2` / `2>&1` duplicate a descriptor; `>&file` writes a file.
        if not (tok == ">&" and (target.isdigit() or target == "-")):
            out.append(target)
        if rest and rest[-1].isdigit():
            rest.pop()                 # the `2` of `2>` is a descriptor, not an arg
        i += 2
    return out, rest


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _after_mode(args: list[str]) -> list[str]:
    return _operands(args)[1:]                     # chmod/chown: mode/owner first


def _option_value(args: list[str], shorts: tuple, longs: tuple) -> list[str]:
    """Values of ``-X VALUE`` / ``--long VALUE`` / ``--long=VALUE`` options."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a in shorts + longs and i + 1 < len(args):
            out.append(args[i + 1])
        else:
            out += [a.split("=", 1)[1] for lo in longs if a.startswith(lo + "=")]
    return out


def _destination(args: list[str]) -> list[str]:
    """cp/mv/rsync/ln/scp: `-t DIR`, else the last of 2+ operands."""
    t = _option_value(args, ("-t",), ("--target-directory",))
    if t:
        return t[:1]
    ops = _operands(args)
    return ops[-1:] if len(ops) >= 2 else []


def _install(args: list[str]) -> list[str]:
    if "-d" in args or "--directory" in args:
        return _operands(args)
    return _destination(args)


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


def _git(args: list[str]) -> list[str]:
    """`git clone URL DIR` writes DIR; `git -C DIR <mutating>` writes DIR."""
    out: list[str] = []
    if "-C" in args and args.index("-C") + 1 < len(args):
        i = args.index("-C")
        sub = next((a for a in args[i + 2:] if not a.startswith("-")), "")
        if sub and sub not in _GIT_READONLY:
            out.append(args[i + 1])
    if "clone" in args:
        ops = _operands(args[args.index("clone") + 1:])
        if len(ops) >= 2:
            out.append(ops[1])
    return out


def _curl(args: list[str]) -> list[str]:
    return _option_value(args, ("-o",), ("--output",))


def _wget(args: list[str]) -> list[str]:
    return _option_value(args, ("-O", "-P"), ("--output-document", "--directory-prefix"))


def _tar(args: list[str]) -> list[str]:
    return _option_value(args, ("-C",), ("--directory",))


def _unzip(args: list[str]) -> list[str]:
    return _option_value(args, ("-d",), ())


_HANDLERS = {"tee": _operands, "sed": _sed_files, "dd": _dd_output, "git": _git,
             "install": _install, "curl": _curl, "wget": _wget, "tar": _tar,
             "unzip": _unzip,
             **dict.fromkeys(_DEST_LAST, _destination),
             **dict.fromkeys(_OPERANDS, _operands),
             **dict.fromkeys(("chmod", "chown", "chgrp"), _after_mode)}


def _strip_prefixes(toks: list[str]) -> list[str]:
    """Look through sudo/env/timeout/xargs/nice/… and VAR=value assignments,
    including the prefixes' own options (`sudo -u root tee /p`)."""
    while toks:
        if "=" in toks[0] and not toks[0].startswith("-"):
            toks = toks[1:]
            continue
        head = os.path.basename(toks[0])
        if head not in _PREFIX_OPT_ARGS:
            return toks
        takes = _PREFIX_OPT_ARGS[head]
        toks = toks[1:]
        while toks and toks[0].startswith("-"):
            toks = toks[2:] if toks[0] in takes else toks[1:]
        if head == "timeout" and toks:
            toks = toks[1:]                       # the duration
    return toks


def _command_targets(toks: list[str], here: str) -> list[str]:
    toks = _strip_prefixes(toks)
    if not toks:
        return []
    cmd, args = os.path.basename(toks[0]), toks[1:]
    if cmd in _SHELLS and "-c" in args and args.index("-c") + 1 < len(args):
        return shell_write_targets(args[args.index("-c") + 1], here)
    handler = _HANDLERS.get(cmd)
    return handler(args) if handler else []


def _segment_writes(toks: list[str], here: str) -> list[str]:
    """Absolute, non-temp paths one command segment writes."""
    redirs, rest = _redirect_targets(toks)
    paths = (_resolve(raw, here) for raw in redirs + _command_targets(rest, here))
    return [p for p in paths if p and not _is_temp(p)]


def shell_write_targets(cmd: str, cwd: str) -> list[str]:
    """Absolute paths ``cmd`` would write, outside temp dirs, in order."""
    here = os.path.realpath(cwd or os.getcwd())
    found: list[str] = []
    for line in _strip_heredocs(cmd):
        for seg in _segments(_tokens(line)):
            if seg[0] == "cd":
                here = _resolve(seg[1] if len(seg) > 1 else "~", here) or here
                continue
            found.extend(p for p in dict.fromkeys(_segment_writes(seg, here))
                         if p not in found)
    return found


def command_of(args: dict) -> str:
    """The command string of a shell tool call (the keys the risk gate reads)."""
    for key in _CMD_ARG_KEYS:
        val = (args or {}).get(key)
        if val:
            return str(val)
    return ""


__all__ = ["SHELL_TOOLS", "shell_write_targets", "command_of"]
