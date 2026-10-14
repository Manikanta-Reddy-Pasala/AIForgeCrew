"""Which repository and branch a chat's git command lands on — checked before
it runs.

A chat that works in two or more repositories (its own worktree and a folder
the user named, or two folders) runs every ``run_command`` from the chat's own
folder: a ``cd`` does not carry over to the next command. A ``git commit`` the
model meant for repo B, written without ``cd B &&`` or ``git -C B``, landed in
repo A. A push that was rejected was "fixed" by switching to some other branch
on the model's own. Nothing checked either, and the user found the commit in
the wrong place afterwards.

This module keeps, per chat, the repositories the chat changed files in and
the branch each one was on when the chat started working there (its PIN).
Stored in ``chat-git-targets.json`` in the config dir, so it survives a
restart. Before a shell command runs, every git call in it is resolved to the
repository it would run in (``cd``, ``git -C`` and ``bash -c`` followed), and:

* a history write (commit, merge, rebase, pull, cherry-pick, revert, am,
  reset) or a push in a repository the chat never changed a file in, while it
  did change files in others, is refused and the right repositories named;
* the same in a repository that is now on another branch than its pin is
  refused;
* a switch to another EXISTING branch in a repository the chat works in, and
  a push to a branch that is neither the current one nor the pin, are refused.

Each of these passes when the user's message of this turn names that
repository (path or folder name) or that branch: the user asked for it.
Making a NEW branch (``checkout -b`` / ``switch -c``) is allowed and becomes
the pin. After a git write runs, the observation says the repository and the
branch it ran on, so the model sees where its commit went.

Fail-open: a path it cannot resolve (``cd $X``), a folder that is not a git
repository, or any error lets the command run as before.
``AIFORGE_CHAT_GIT_TARGET_GUARD=0`` turns it off.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from aiforge_core.runtime import shell_writes as sw

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_TOPS: dict[str, "str | None"] = {}
_TOPS_MAX = 512

#: Subcommands that write the history of the branch that is checked out.
_HISTORY_WRITES = frozenset({"commit", "merge", "rebase", "pull", "cherry-pick",
                             "revert", "am", "reset"})
_SWITCHES = frozenset({"checkout", "switch"})
#: Git options whose next word is their value, not the subcommand.
_VALUE_OPTS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace",
                         "--exec-path", "--config-env"})
_CHDIR = frozenset({"cd", "pushd", "popd"})
#: A heredoc start (``<<EOF``, ``<<-'EOF'``) — not a here-string ``<<<``.
_HEREDOC_RE = re.compile(r"(?<!<)<<-?(?!<)\s*['\"]?([A-Za-z_]\w*)")
#: ``git checkout`` / ``switch`` options that take a value.
_SWITCH_VALUE_OPTS = frozenset({"--conflict", "--pathspec-from-file", "--orphan"})
_NEW_BRANCH = frozenset({"-b", "-B", "-c", "-C", "--create", "--force-create"})
#: ``git push`` options that take a value.
_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack",
                              "--exec"})
#: Flags that end or abandon an operation already under way: never checked.
_FINISHERS = frozenset({"--continue", "--abort", "--skip", "--quit"})
#: Files in the git dir while a rebase / merge / pick is under way (HEAD may be
#: detached then: that is not a wrong branch).
_BUSY = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
         "REVERT_HEAD")
#: Shell operators: a token made only of these is split into them (shlex
#: returns ``);`` as one token).
_OPS_RE = re.compile(r"&&|\|\||;;|\|&|[();&|{}]")
_SEP = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "{", "}"})


def is_write(sub: str) -> bool:
    """A git subcommand that changes a branch or sends it (checked here)."""
    return sub in _HISTORY_WRITES or sub == "push" or sub in _SWITCHES


def _owner(session_id):
    from aiforge_core.runtime.repo_roles import owner
    return owner(session_id)


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_GIT_TARGET_GUARD", "1").strip().lower() \
        not in ("0", "false", "no", "off")


# ── the per-chat record ──────────────────────────────────────────────────────

def _path() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(str(config_dir()), "chat-git-targets.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt: nothing recorded
        return {}


def _save(data: dict) -> None:
    from aiforge_core.config import _atomic
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        _atomic.write_text(_path(), json.dumps(data, indent=1))
    except Exception as exc:  # noqa: BLE001 — the check simply knows less
        log.debug("git targets not saved: %s", exc)


def pins(session_id) -> dict:
    """``{repository top folder: branch}`` for the repositories this chat
    changed files in ("" = no branch: a detached HEAD)."""
    session_id = _owner(session_id)
    if session_id is None:
        return {}
    with _LOCK:
        got = _load().get(str(session_id))
    return {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(got, dict) else {}


def _pin(session_id, updates: dict, *, move: bool = False) -> None:
    """Record ``{top: branch}``. A repository already recorded keeps its pin
    unless ``move`` (a git command the check allowed changed the branch)."""
    session_id = _owner(session_id)
    if session_id is None or not updates:
        return
    with _LOCK:
        data = _load()
        have = data.get(str(session_id))
        have = dict(have) if isinstance(have, dict) else {}
        changed = False
        for top, branch in updates.items():
            if top not in have or (move and have[top] != branch) \
                    or (branch and not have[top]):
                have[top] = branch
                changed = True
        if changed:
            data[str(session_id)] = have
            _save(data)


def forget(session_id=None) -> None:
    """Drop a deleted chat's record (``None``: every chat's)."""
    with _LOCK:
        data = {} if session_id is None else _load()
        if session_id is None or data.pop(str(session_id), None) is not None:
            _save(data)


# ── git ──────────────────────────────────────────────────────────────────────

def _git(args, cwd: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return ""
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def toplevel(folder: "str | None") -> "str | None":
    """The top folder of the working tree ``folder`` is in (a linked worktree
    is its own), or None. Cached: a folder does not change repository."""
    if not folder:
        return None
    folder = os.path.realpath(folder)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent
    cached = _TOPS.get(folder)
    # A folder that became a repository of its own (``git init``) since.
    if cached and (cached == folder or not os.path.exists(os.path.join(folder, ".git"))):
        return cached
    top = _git(["rev-parse", "--show-toplevel"], folder)
    top = os.path.realpath(top) if top else None
    if top:
        if len(_TOPS) >= _TOPS_MAX:
            _TOPS.clear()
        _TOPS[folder] = top
    return top


def branch(top: str) -> str:
    """The branch checked out in ``top`` ("" for a detached HEAD)."""
    return _git(["symbolic-ref", "--short", "-q", "HEAD"], top)


def busy(top: str) -> bool:
    """A rebase, merge, cherry-pick or revert is under way in ``top``."""
    args = ["rev-parse"]
    for name in _BUSY:
        args += ["--git-path", name]
    for p in _git(args, top).splitlines():
        if p and os.path.exists(p if os.path.isabs(p) else os.path.join(top, p)):
            return True
    return False


def _previous(top: str, spec: str = "@{-1}") -> str:
    """The branch ``git checkout -`` (or ``@{-N}``) goes back to."""
    return _git(["rev-parse", "--abbrev-ref", spec], top)


def _branch_exists(top: str, name: str) -> bool:
    """A local branch, or a remote one ``git checkout <name>`` would track."""
    if not name or name.startswith("-"):
        return False
    if _git(["rev-parse", "--verify", "-q", f"refs/heads/{name}"], top):
        return True
    remotes = _git(["for-each-ref", "--format=%(refname:short)",
                    f"refs/remotes/*/{name}"], top)
    return bool(remotes)


# ── reading a command ────────────────────────────────────────────────────────

@dataclass
class GitCall:
    sub: str
    args: list
    folder: "str | None"        # None: could not tell (``cd $X``)


def _resolve_dir(raw: str, here: "str | None") -> "str | None":
    if here is None:
        # An absolute path does not need to know where we were.
        return sw._resolve(raw, os.sep) if raw.startswith(("/", "~")) else None
    return sw._resolve(raw, here)


def _git_parts(toks: list, here: "str | None") -> "GitCall | None":
    """``toks`` as a git call, past its global options; None when not git."""
    if not toks or os.path.basename(toks[0]) != "git":
        return None
    folder = here
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        if opt == "-C" and i + 1 < len(toks):
            folder = _resolve_dir(toks[i + 1], folder)
            i += 2
        elif opt == "--work-tree" and i + 1 < len(toks):
            folder = _resolve_dir(toks[i + 1], here)
            i += 2
        elif opt.startswith("--work-tree="):
            folder = _resolve_dir(opt.split("=", 1)[1], here)
            i += 1
        elif opt == "--git-dir":
            folder = None                     # somewhere else entirely: not ours
            i += 2
        elif opt in _VALUE_OPTS:
            i += 2
        elif opt.startswith("--git-dir="):
            folder = None                     # somewhere else entirely: not ours
            i += 1
        else:
            i += 1
    if i >= len(toks):
        return None
    return GitCall(toks[i], list(toks[i + 1:]), folder)


def _strip_heredocs(cmd: str) -> list:
    """The command's lines without heredoc bodies (data, not commands)."""
    out: list = []
    ending: list = []
    for line in (cmd or "").splitlines():
        if ending:
            if line.strip() == ending[0]:
                ending.pop(0)
            continue
        out.append(line)
        ending = _HEREDOC_RE.findall(line)
    return out


def _one_line(text: str) -> str:
    """Newlines outside quotes become ``;`` (a quoted multi-line commit message
    stays one word); a ``\\``-newline continuation joins the lines; ``#``
    comments are dropped. A ``$'…'`` quote becomes one placeholder word."""
    out: list = []
    q = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and q != "'":
            if text[i + 1] == "\n":
                i += 2                    # line continuation: no break at all
                continue
            out.append(text[i:i + 2])
            i += 2
            continue
        if q:
            if ch == q:
                q = None
        elif ch == "$" and i + 1 < n and text[i + 1] == "'":
            # ANSI-C quote: ``\'`` does not end it, and shlex cannot read it.
            # It is data (a message): one placeholder word stands for it.
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            out.append('"_"')
            i = j + 1
            continue
        elif ch in "'\"":
            q = ch
        elif ch == "\n":
            ch = ";"
        elif ch == "#" and (not out or out[-1] in " \t;"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_ops(toks: list) -> list:
    out: list = []
    for t in toks:
        if t and t != "{}" and all(c in "();&|{}" for c in t):
            out.extend(_OPS_RE.findall(t))
        else:
            out.append(t)
    return out


def _shell_script(rest: list) -> "str | None":
    """The script of ``bash -c '…'`` / ``bash -lc '…'`` / ``sh -ec '…'``."""
    for i, a in enumerate(rest[1:], 1):
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            return rest[i + 1] if i + 1 < len(rest) else None
        if not a.startswith("-"):
            return None                   # ``bash script.sh``: not inline
    return None


def calls(cmd: str, here: "str | None") -> list:
    """Every git call in ``cmd``, in order, with the folder it runs in. A ``cd``
    inside ``( … )`` ends with the subshell."""
    try:
        text = os.path.expandvars(cmd or "")
    except Exception:  # noqa: BLE001
        text = cmd or ""
    text = _one_line("\n".join(_strip_heredocs(text)))
    out: list = []
    state = {"here": here, "prev": None, "dirs": []}
    stack: list = []
    seg: list = []

    def flush():
        toks = list(seg)
        seg.clear()
        while toks and toks[0] in sw._KEYWORDS:
            toks = toks[1:]
        if not toks or toks[0] in sw._HEADERS:
            return
        if toks[0] in sw._CONDITIONS:
            toks = toks[1:]
        _redirs, rest = sw._redirect_targets(toks)
        rest = sw._strip_prefixes(rest)
        if not rest:
            return
        head = os.path.basename(rest[0])
        cur = state["here"]
        if head == "popd":
            if state["dirs"]:
                state["here"] = state["dirs"].pop()
            return
        if head in _CHDIR:
            ops = [t for t in rest[1:] if not t.startswith("-") or t == "-"]
            if head == "pushd":
                state["dirs"].append(cur)
            if ops and ops[0] == "-":
                state["here"], state["prev"] = state["prev"], cur
            else:
                state["here"], state["prev"] = _resolve_dir(ops[0] if ops else "~", cur), cur
            return
        script = _shell_script(rest) if head in sw._SHELLS else None
        if script is not None:
            out.extend(calls(script, cur))
            return
        found = _git_parts(rest, cur)
        if found is not None:
            out.append(found)

    for t in _split_ops(sw._tokens(text)):
        if t == "(":
            flush()
            stack.append((state["here"], state["prev"], list(state["dirs"])))
        elif t == ")":
            flush()
            if stack:
                state["here"], state["prev"], state["dirs"] = stack.pop()
        elif t in _SEP:
            flush()
        else:
            seg.append(t)
    flush()
    return out


def _operands(args: list, value_opts=frozenset()) -> list:
    """The non-option words of ``args`` (up to ``--``)."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            break
        if a in value_opts:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _switch_target(top: str, call: GitCall) -> "tuple[str, str]":
    """``(new branch, existing branch)`` a checkout/switch moves to; both ""
    when it moves to no branch (a file checkout, a detached commit)."""
    args = call.args

    def made_or_reset(name: str, force: bool) -> "tuple[str, str]":
        # ``-B main`` on a branch that exists moves to (and resets) it.
        if force and _branch_exists(top, name):
            return "", name
        return name, ""

    for i, a in enumerate(args):
        if a in _NEW_BRANCH and i + 1 < len(args):
            return made_or_reset(args[i + 1], a in ("-B", "-C", "--force-create"))
        for flag in ("--create=", "--force-create="):
            if a.startswith(flag):
                return made_or_reset(a[len(flag):], flag == "--force-create=")
    if "--" in args or "--detach" in args or "-d" in args:
        return "", ""
    if "-" in args:
        return "", _previous(top)
    ops = _operands(args, _SWITCH_VALUE_OPTS)
    if "-t" in args or "--track" in args or any(a.startswith("--track=") for a in args):
        if len(ops) == 1 and "/" in ops[0]:     # ``--track origin/x`` makes x
            local = ops[0].split("/", 1)[1]
            return ("", local) if _branch_exists(top, local) else (local, "")
    if len(ops) != 1:
        return "", ""             # paths, or ``checkout <commit> <path>``
    name = ops[0]
    if name.startswith("@{-"):
        return "", _previous(top, name)
    if call.sub == "switch" or _branch_exists(top, name):
        return "", name
    return "", ""


def _push_targets(top: str, args: list) -> list:
    """The remote branches an explicit ``git push <remote> <refspec>…`` names
    (``HEAD:main`` → ``main``, ``feat/x`` → ``feat/x``); tags are not branches."""
    ops = _operands(args, _PUSH_VALUE_OPTS)
    specs = ops[1:]
    if "--tags" in args or (specs and specs[0] == "tag"):
        return []
    out = []
    for spec in specs:
        spec = spec.lstrip("+")
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        if dst and dst not in ("HEAD", "@") and not dst.startswith("refs/") \
                and not _git(["rev-parse", "--verify", "-q", f"refs/tags/{dst}"], top):
            out.append(dst)
    return out


# ── what the user asked for ──────────────────────────────────────────────────

#: After a name: not more of a word, a path, or a file extension (``main.py``).
_END = r"(?![\w/-]|\.\w)"


def _word(name: str, text: str) -> bool:
    return bool(name) and re.search(
        r"(?<![\w./-])" + re.escape(name) + _END, text or "",
        re.IGNORECASE) is not None


#: Words that say the next word IS a branch: "push main", "checkout develop".
_BRANCH_STRONG = r"(?:branch(?:es)?|push(?:ed)?|checkout|switch|merge|rebase|base)"
#: Words that only point at something ("to main", "on develop"): a branch only
#: when the sentence ends or goes on right after it — "build on main logic"
#: names no branch.
_BRANCH_WEAK = r"(?:to|into|onto|on|from|off)"
_THEN = (r"(?=\s*(?:$|[,;:!?)]|\.(?!\w)|\s(?:branch|and|then|please|too|now|"
         r"instead|first|again|as)\b))")


def branch_named(name: str, said: str) -> bool:
    """The user named branch ``name``. A name like ``feat/x`` or ``release-2``
    counts anywhere; a plain word (``main``, ``test``) only in back-quotes or
    next to a branch word ("push to main", "the develop branch") — "the main
    issue" names no branch."""
    if not name or not said:
        return False
    if re.search(r"[/_.\-\d]", name):
        return _word(name, said)
    esc = re.escape(name)
    return any(re.search(p, said, re.IGNORECASE) for p in (
        rf"`{esc}`",
        rf"\b{_BRANCH_STRONG}\s+(?:the\s+)?{esc}{_END}",
        rf"\b{_BRANCH_WEAK}\s+(?:the\s+)?{esc}{_END}{_THEN}",
        rf"(?<![\w/-]){esc}\s+branch\b"))


def repo_named(top: str, said: str) -> bool:
    """The user named this repository: its path, or its folder name (for a
    chat worktree: the project's)."""
    if not said:
        return False
    from aiforge_core.runtime import chat_worktree as cw
    main = cw.main_repo_of(top)
    mark = os.sep + cw._IN_REPO + os.sep
    if main is None and mark in top:
        main = top.split(mark, 1)[0]        # a chat worktree inside its project
    return any(folder_named(f, said) for f in {top, main or top}) \
        if main is None or main == top else folder_named(main, said)


#: Words that say the next word is a folder / repository.
_FOLDER_CTX = r"(?:in|into|to|from|of|repo|repository|project|folder|directory|dir)"


def folder_named(folder: str, said: str) -> bool:
    """The user named ``folder``: its full path (not a longer one it is the
    start of), or its folder name in back-quotes, as a path end (``…/name``),
    or next to a folder word ("in rust-port", "the api repo"). A bare common
    word ("the backend is slow") does not count."""
    if not folder or not said:
        return False
    folder = folder.rstrip(os.sep)
    if re.search(re.escape(folder) + r"(?![\w.-])", said):
        return True
    name = os.path.basename(folder)
    if len(name) < 3:
        return False
    esc = re.escape(name)
    return any(re.search(p, said, re.IGNORECASE) for p in (
        rf"`{esc}`",
        rf"/{esc}{_END}",
        rf"\b{_FOLDER_CTX}\s+(?:the\s+)?{esc}{_END}",
        rf"(?<![\w./-]){esc}\s+(?:repo|repository|project|folder|directory|dir)\b"))


# ── the check ────────────────────────────────────────────────────────────────

def _show(pinned: dict) -> str:
    return "; ".join(f"{top} (branch `{b or 'detached HEAD'}`)" for top, b in pinned.items())


def _refusal(kind: str, text: str) -> dict:
    return {"ok": False, "blocked": "git_target", "reason": kind, "error": text}


def check(session_id, cmd: str, here: "str | None", said: str = "") -> "dict | None":
    """The refusal for ``cmd`` (a shell command run from ``here``), or None when
    it may run. ``said``: the user's words in this turn."""
    if session_id is None or not enabled() or "git" not in (cmd or ""):
        return None
    try:
        return _check(session_id, cmd, here, said)
    except Exception as exc:  # noqa: BLE001 — a reader bug never blocks a command
        log.debug("git target check skipped: %s", exc)
        return None


def _check(session_id, cmd, here, said):
    gcalls = calls(cmd, os.path.realpath(here) if here else None)
    if not gcalls:
        return None
    pinned = pins(session_id)
    now: dict = {}                 # top → the branch it will be on at this call
    made: set = set()              # (top, branch) made by this command
    # Files this same command changes make their repository one it works in.
    edits = {toplevel(p) for p in edited_paths(cmd, here or os.getcwd())}
    for c in gcalls:
        if not is_write(c.sub) or _FINISHERS & set(c.args):
            continue
        top = toplevel(c.folder)
        if not top:
            continue
        cur = now[top] if top in now else branch(top)
        pin = pinned.get(top)
        if c.sub in _SWITCHES:
            new, target = _switch_target(top, c)
            if new:
                now[top] = new
                made.add((top, new))
            elif target:
                if pin and target != pin and not branch_named(target, said):
                    return _refusal("branch_switch", (
                        f"Refused: `git {c.sub} {target}` would move {top} off branch "
                        f"`{pin or 'detached HEAD'}`, the branch this chat's work there is "
                        f"on, and the user did not name `{target}` in this turn. Stay on "
                        f"`{pin}`. If another branch is really needed, ASK the user which "
                        "one in FINAL; do not pick one yourself."))
                now[top] = target
            continue
        if pinned and top not in pinned and top not in edits \
                and not repo_named(top, said) and not _work_folder(session_id, top):
            return _refusal("wrong_repo", (
                f"Refused: this `git {c.sub}` would run in {top}, but this chat has not "
                f"changed any file there. The repositories this chat changed files in: "
                f"{_show(pinned)}. Every run_command starts in the chat's folder; a `cd` "
                "in an earlier command does not carry over. Run it in the right one: "
                f"`git -C <repository> {c.sub} …` (or `cd <repository> && git {c.sub} …` "
                "in the same command). If the user really wants it in "
                f"{top}, ask them in FINAL."))
        if pin and cur != pin and (top, cur) not in made \
                and not branch_named(cur, said) and not busy(top):
            return _refusal("wrong_branch", (
                f"Refused: {top} is on branch `{cur or 'detached HEAD'}`, but this chat's "
                f"work there is on `{pin}` (where it started). `git {c.sub}` would land "
                f"on the wrong branch. Something switched it (another chat, the user, or "
                f"an earlier command). Do not commit or push here: tell the user in FINAL "
                f"that {top} is now on `{cur}` and ask whether to switch back to `{pin}`."))
        if c.sub == "push" and ({"--all", "--mirror"} & set(c.args)) and not re.search(
                r"\ball (?:the )?branches\b|\bmirror\b", said or "", re.IGNORECASE):
            return _refusal("push_target", (
                f"Refused: `git push {' '.join(a for a in c.args if a in ('--all', '--mirror'))}`"
                f" sends every branch of {top}, and the user did not ask for that. Push "
                f"the current branch only (`git -C {top} push -u origin {cur or 'HEAD'}`)."))
        if c.sub == "push":
            for dst in _push_targets(top, c.args):
                if dst not in (cur, pin) and (top, dst) not in made \
                        and not branch_named(dst, said):
                    return _refusal("push_target", (
                        f"Refused: this push sends to branch `{dst}`, but {top} is on "
                        f"`{cur or 'detached HEAD'}` and the user did not name `{dst}` "
                        f"in this turn. Push the current branch (`git -C {top} push -u "
                        f"origin {cur or 'HEAD'}`), or ask the user which branch to push to."))
    return None


def _work_folder(session_id, top: str) -> bool:
    """The chat recorded ``top``'s repository as one it works in."""
    from aiforge_core.runtime import repo_roles
    return repo_roles._repo_role(top, session_id) == repo_roles.WORK


# ── after it ran ─────────────────────────────────────────────────────────────

def _finished(result) -> bool:
    """The command ran to its end (not refused, not still running, not handed
    off to the background — a background start has no exit code)."""
    return isinstance(result, dict) and not result.get("blocked") \
        and not result.get("running") and "code" in result


def _ok(name: str, result) -> bool:
    if not isinstance(result, dict) or result.get("ok") is False or result.get("blocked"):
        return False
    if name in sw.SHELL_TOOLS:
        return _finished(result) and result.get("code") in (0, None)
    return True


def command_base(cwd) -> str:
    """The folder run_command runs a command in (file tools use ``cwd``)."""
    from aiforge_core.runtime.chat_agent._shell import _workspace_root
    return str(_workspace_root() or cwd or "")


def is_scratch(path: str) -> bool:
    """A chat's scratch folder (helper files, never committed). It sits next
    to the chat's worktree, inside the project folder when the worktree does."""
    from aiforge_core.runtime import chat_worktree as cw
    path = str(path)
    if re.search(re.escape(os.sep + cw._IN_REPO + os.sep) + r"[^/]+"
                 + re.escape(os.sep + cw._SCRATCH) + r"(?:/|$)", path):
        return True
    try:                                  # worktrees placed in AIForge's own folder
        root = os.path.realpath(cw._root())
    except Exception:  # noqa: BLE001
        return False
    return re.match(re.escape(root + os.sep) + r"[^/]+/[^/]+/" + re.escape(cw._SCRATCH)
                    + r"(?:/|$)", os.path.realpath(path)) is not None


def edited_paths(cmd: str, here: str) -> list:
    """The paths a shell command writes, without the folder a ``git -C <dir>``
    names (that is where git runs, not a file it changes) and without scratch."""
    gitdirs = {os.path.realpath(c.folder) for c in calls(cmd, here) if c.folder}
    return [p for p in sw.shell_write_targets(cmd, here)
            if os.path.realpath(p) not in gitdirs and not is_scratch(p)]


def after(session_id, name: str, args: dict, result, cwd: "str | None") -> str:
    """Record where the chat worked and return a line for the observation that
    says where its git writes landed ("" for none). A git command that failed
    half way still gets its line, and a branch it made and moved to becomes the
    pin (its retry must not be refused as the wrong branch)."""
    if session_id is None or not enabled():
        return ""
    try:
        if _ok(name, result):
            return _after(session_id, name, args or {}, cwd, failed=False)
        if name in sw.SHELL_TOOLS and _finished(result):
            return _after(session_id, name, args or {}, cwd, failed=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("git target record skipped: %s", exc)
    return ""


def _after(session_id, name, args, cwd, *, failed: bool):
    from aiforge_core.runtime.tools.mutating import writes_files
    if writes_files(name, args):
        from aiforge_core.runtime.chat_agent._turn._progress import _full, _named_paths
        here = os.path.realpath(cwd) if cwd else None
        _work_in(session_id, [_full(p, here) for p in _named_paths(args, None)])
        return ""
    if name not in sw.SHELL_TOOLS:
        return ""
    cmd = sw.command_of(args)
    if not cmd:
        return ""
    here = os.path.realpath(command_base(cwd) or os.getcwd())
    if not failed:
        _work_in(session_id, edited_paths(cmd, here))
    if "git" not in cmd:
        return ""
    lines, moved = [], {}
    for c in calls(cmd, here):
        if not is_write(c.sub):
            continue
        top = toplevel(c.folder)
        if not top:
            continue
        b = branch(top)
        if b and not busy(top):
            if not failed:
                moved[top] = b     # never pin a detached HEAD or a half-done rebase
            elif c.sub in _SWITCHES and b in _switch_target(top, c):
                moved[top] = b     # the branch it moved to (allowed) is where it is now
        verb = "was in the command, for" if failed else "ran in"
        line = f"`git {c.sub}` {verb} {top} (now on branch `{b or 'detached HEAD'}`)"
        if line not in lines:
            lines.append(line)
    # The check let these through: the branch each repository is on now is
    # the one the chat works on there.
    _pin(session_id, moved, move=True)
    if not lines:
        return ""
    head = ("[git] the command FAILED, so these git steps may not all have run — check "
            "with `git -C <repo> log -1` / `status`: " if failed else "[git] ")
    return head + "; ".join(lines) + ". Check this is the repository and branch the " \
        "work belongs to."


def _work_in(session_id, paths) -> None:
    found: dict = {}
    pinned = pins(session_id) if paths else {}
    for p in paths or ():
        if is_scratch(p):
            continue
        top = toplevel(os.path.dirname(p) if not os.path.isdir(p) else p)
        # A repository already pinned keeps its pin: no git call for it
        # (one detached when pinned is re-read until it is on a branch).
        if top and top not in found and not pinned.get(top):
            found[top] = branch(top)
    _pin(session_id, found)


def prompt_note(session_id) -> str:
    """The repositories (and branches) this chat works in, for the turn's
    context; "" for fewer than two (one repository cannot be confused)."""
    pinned = pins(session_id)
    if len(pinned) < 2:
        return ""
    return ("GIT TARGETS: this chat changed files in more than one repository — "
            + _show(pinned) + ". Every run_command starts in the chat's folder; a `cd` "
            "does not carry over to the next command. Name the repository in EVERY git "
            "command (`git -C <repository> …`) and commit each change in the repository "
            "its files are in, on the branch shown.")


__all__ = ["after", "branch", "branch_named", "calls", "check", "command_base",
           "edited_paths", "enabled", "folder_named", "forget", "is_scratch", "is_write",
           "pins", "prompt_note", "repo_named", "toplevel"]
