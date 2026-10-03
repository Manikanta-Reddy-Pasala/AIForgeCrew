"""What a chat's tool calls CREATED or STARTED, read from the calls themselves.

The cleanup inventory (:mod:`aiforge_core.runtime.cleanup_inventory`) lists
what a chat left behind. This module is its detector: it replays the tool
steps in order and returns, for each thing a SUCCESSFUL call made, one entry
(what, where, how to undo), and the keys of the entries a later successful
call removed again (``pip uninstall``, ``docker rm``, ``git branch -D``).

Only facts are used: the tool name, its arguments and its result. Nothing the
model wrote in a reply is read. A command that failed made nothing.

**An undo is offered only for what the call PROVES the chat made.** The list is
read by a model that may be told "clean up", so a wrong entry is not a cosmetic
problem: ``rm -rf`` on a directory that was there before, ``pip uninstall`` of
a package that was already installed, ``docker compose down`` on a stack that
was already up. So:

* **Only a plain command is read.** Parts joined by ``&&`` (each may pipe its
  output into a filter). Anything else — ``;``, ``||``, a trailing ``&``, a
  subshell, ``$(…)``, a heredoc, more than one line — is not interpreted at
  all: text inside a heredoc is not a command that ran, and ``a || b``
  succeeding says nothing about ``a``. Quotes are respected, so
  ``sh -c "pip install x"`` and ``docker exec c pip install x`` are one
  command of ``sh`` / ``docker``, not a pip install on this machine.
* **Proof by exit status**: ``mkdir`` without ``-p``, ``git clone <dest>``,
  ``git checkout -b``, ``git worktree add`` and ``docker run --name`` each FAIL
  on something that already exists, so success means it was made. A part
  that pipes its output has the filter's exit status, so it proves nothing.
* **Proof by the installer's own words**: pip's ``Successfully installed``
  (minus what it says it replaced), npm's ``added N packages``, apt's ``NEW
  packages`` list, compose's ``Created``.
* **A file tool whose result says ``created``** (the path did not exist).

Something the chat wrote to without that proof (``mkdir -p``, ``> file``, a
patch to a file, a package among several installed at once) is listed with NO
undo, or left out. An undo that depends on where it runs carries its folder
(``cd <dir> && …``); one that depends on which Python environment is active
is not offered at all.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile

from aiforge_core.runtime.tools.mutating import writes_files

#: Tools whose arguments carry a shell command.
CMD_TOOLS = frozenset({"run_command", "bash", "run_shell", "shell", "run",
                       "serve"})
#: Tools that report on a command started earlier (by job id).
JOB_TOOLS = frozenset({"command_wait", "command_output", "command_kill"})

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SPEC = re.compile(r"[<>=!~\[@ ].*$")
_HEX = re.compile(r"^[0-9a-f]{12,64}$")
_PUNCT = frozenset("();<>|&")
_REDIRECTS = frozenset({">", ">>", "<", ">&", "&>", "&>>", ">|", "<&"})
_PIP_DONE = re.compile(r"Successfully installed (.+)")
_PIP_REPLACED = re.compile(r"Found existing installation: (\S+)")
_UV_ADDED = re.compile(r"(?m)^\s*\+ ([A-Za-z0-9_.\-]+)==")
_UV_REMOVED = re.compile(r"(?m)^\s*- ([A-Za-z0-9_.\-]+)==")
_NPM_ADDED = re.compile(r"\badded (\d+) packages?\b")
_NPM_TOUCHED = re.compile(r"\b(?:changed|removed) \d+ packages?\b")
_APT_NEW = re.compile(r"The following NEW packages will be installed:\s*\n((?:[ \t]+.*\n?)+)")
_COMPOSE_CREATED = re.compile(r"\b(?:Created|Creating)\b")
_COMPOSE_STARTED = re.compile(r"\b(?:Started|Starting)\b")
_COMPOSE_WAS_UP = re.compile(r"\bRunning\b|is up-to-date")
_STASH_SAVED = re.compile(r"Saved working directory and index state (.+)")
#: Commands after which "which pip / which node" is no longer the plain one.
_ENV_SWITCH = frozenset({"source", ".", "activate", "conda", "nvm", "pyenv", "export",
                         "workon", "asdf"})
_PIP_ENV_HINT = "uninstall it with the pip of the environment it went into"

_VAL = r"""(?:"[^"]*"|'[^']*'|[^\s'"]+)"""          # a value, quoted or bare
_SECRETS = (
    # user:password@host — the password may itself contain '/'
    (re.compile(r"(://[^\s/:@]+:)[^\s@]+@"), r"\1***@"),
    # NAME_TOKEN=..., "password": "...", X-Api-Key: ... (quoted or bare values)
    (re.compile(r"(?i)([\w-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PWD|API[_-]?KEY|APIKEY|"
                r"ACCESS[_-]?KEY|PRIVATE[_-]?KEY|COOKIE|CREDENTIALS?)[\w-]*[\"']?"
                r"\s*[=:]\s*)" + _VAL), r"\1***"),
    (re.compile(r"\b(ghp_|gho_|ghs_|github_pat_|glpat-|sk-|hf_|xox[baprs]-|AKIA)[\w-]{6,}"),
     r"\1***"),
    # Authorization: Bearer|Basic|Token <value>, and a bare 'bearer <value>'
    (re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?\s*\w+\s+)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key|apikey|auth)[= ])" + _VAL),
     r"\1***"),
    (re.compile(r"((?:^|\s)(?:-u|--user)[= ]\s*[^\s:'\"]+:)(?!\d+\b)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(\bsshpass\s+-p\s*)\S+"), r"\1***"),
    # mysql -pSECRET / mysql -p SECRET, docker login -p X, redis-cli -a X
    (re.compile(r"(\b(?:mysql|mysqldump|mariadb|mysqladmin)\b[^\n|;&]*?\s-p)\S+"), r"\1***"),
    (re.compile(r"(\bdocker\s+login\b[^\n|;&]*?\s(?:-p|--password)[= ]?\s*)\S+"), r"\1***"),
    (re.compile(r"(\bredis-cli\b[^\n|;&]*?\s-a\s+)\S+"), r"\1***"),
)


def redact(text: str) -> str:
    """``text`` with the credentials a command line commonly carries masked.
    The log and the inventory are shown to people (the API, the final line)."""
    out = str(text or "")
    for rx, repl in _SECRETS:
        out = rx.sub(repl, out)
    return out


def temp_roots() -> tuple:
    roots = {"/tmp", "/var/tmp", "/private/tmp", "/dev/shm"}
    try:
        roots.add(os.path.realpath(tempfile.gettempdir()))
        roots.add(tempfile.gettempdir())
    except Exception:  # noqa: BLE001
        pass
    return tuple(sorted(r.rstrip("/") for r in roots if r and r != "/"))


def is_temp_path(path: str) -> bool:
    p = str(path or "")
    return any(p.startswith(r + "/") for r in temp_roots())


class Part:
    """One ``&&``-joined part of a plain command."""

    __slots__ = ("toks", "here", "sudo", "redirects", "piped", "env_switched")

    def __init__(self, toks, here, sudo, redirects, piped, env_switched) -> None:
        self.toks, self.here, self.sudo = toks, here, sudo
        self.redirects, self.piped, self.env_switched = redirects, piped, env_switched


def _split(cmd: str) -> "list | None":
    """``[(argv, redirects, piped)]`` for a command made only of ``&&`` parts
    (see the module doc); None for anything else."""
    cmd = str(cmd or "")
    if not cmd.strip() or any(x in cmd for x in ("\n", "`", "$(", "<<")):
        return None
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    parts: list = []
    argv: list = []
    redirects: list = []
    piped = False
    in_filter = False               # after a ``|``: the rest of the part is a filter
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and all(c in _PUNCT for c in tok):
            if tok == "&&":
                parts.append((argv, redirects, piped))
                argv, redirects, piped, in_filter = [], [], False, False
            elif tok == "|":
                piped, in_filter = True, True
            elif tok in _REDIRECTS:
                if i + 1 >= len(tokens):
                    return None
                if not in_filter:
                    if argv and argv[-1] in ("1", "2"):
                        argv.pop()          # the fd of ``2>file``
                    redirects.append((tok, tokens[i + 1]))
                i += 1
            else:
                return None                 # ; || & ( ) and the like
        elif not in_filter:
            argv.append(tok)
        i += 1
    parts.append((argv, redirects, piped))
    return [p for p in parts if p[0]]


def plain_parts(cmd: str, cwd: str) -> list:
    """The :class:`Part` s of ``cmd``, [] when it is not a plain command. A
    ``cd X`` part moves the cwd of what follows; ``sudo`` and ``VAR=x``
    prefixes are dropped (``sudo`` is reported)."""
    split = _split(cmd)
    if not split:
        return []
    out: list = []
    here = cwd
    switched = False
    for argv, redirects, piped in split:
        toks = list(argv)
        while toks and _ENV_ASSIGN.match(toks[0]):
            toks = toks[1:]
        sudo = bool(toks) and toks[0] == "sudo"
        if sudo:
            toks = toks[1:]
            while toks and toks[0].startswith("-"):
                toks = toks[1:]
        if not toks:
            continue
        head = os.path.basename(toks[0])
        if head == "cd" and len(toks) >= 2:
            here = os.path.normpath(os.path.join(here or "", os.path.expanduser(toks[1])))
            continue
        if head in _ENV_SWITCH:
            switched = True
            continue
        out.append(Part(toks, here, sudo, redirects, piped, switched))
    return out


def _plain(args: list, valued: tuple = ()) -> list:
    """The non-flag words of ``args``; a flag in ``valued`` takes the next word."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in valued:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _flag_value(args: list, *names: str) -> str:
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(n + "="):
                return a[len(n) + 1:]
    return ""


def _entry(kind: str, key: str, what: str, where: str = "", undo: str = "",
           **probe) -> dict:
    return {"kind": kind, "key": key, "what": what[:160], "where": where[:200],
            "undo": undo[:300], **probe}


def _pkg_name(spec: str) -> str:
    return _SPEC.sub("", spec).strip() or spec


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def _output(res: dict) -> str:
    return "\n".join(str(res.get(k) or "") for k in ("stdout", "stderr", "new_output"))


def _pip_new(out: str) -> dict:
    """``{normalised name: name}`` pip (or uv) says it NEWLY installed: what it
    lists as installed, minus what it says it replaced (an upgrade)."""
    names: dict[str, str] = {}
    for line in _PIP_DONE.findall(out):
        for tok in line.split():
            name = tok.rsplit("-", 1)[0] if "-" in tok else tok
            names[_norm(name)] = name
    for name in _UV_ADDED.findall(out):
        names[_norm(name)] = name
    for old in _PIP_REPLACED.findall(out) + _UV_REMOVED.findall(out):
        names.pop(_norm(old), None)
    return names


class Replay:
    """Entries found and keys resolved, in the order the steps happened.

    ``jobs`` carries, from one replay to the next, the commands that were
    still running when they were last seen (job id -> ``[cmd, cwd,
    persistent shell?]``)."""

    def __init__(self, cwd: str = "", jobs: "dict | None" = None) -> None:
        self.cwd = cwd or ""
        self.found: dict[str, dict] = {}
        self.resolved: set = set()           # keys a later call removed again
        self.jobs: dict[str, list] = {str(k): list(v) for k, v in (jobs or {}).items()
                                      if isinstance(v, (list, tuple)) and len(v) == 3}

    # -- bookkeeping --

    def _temp_path(self, full: str) -> bool:
        """Under a temp root and NOT inside the chat's own folder: a project
        that lives under /tmp is a project, not a temp artefact."""
        if not is_temp_path(full) or full in temp_roots():
            return False
        root = self.cwd.rstrip("/")
        return not (root and (full == root or full.startswith(root + "/")))

    def _at(self, here: str, cmd: str) -> str:
        """``cmd`` as it must be run to hit the same place: with its folder
        when that is not the chat's own."""
        if here and os.path.normpath(here) != os.path.normpath(self.cwd or here):
            return f"cd {shlex.quote(here)} && {cmd}"
        return cmd

    def add(self, e: dict) -> None:
        self.found.pop(e["key"], None)       # newest position wins
        self.found[e["key"]] = e
        self.resolved.discard(e["key"])

    def resolve(self, key: str) -> None:
        self.found.pop(key, None)
        self.resolved.add(key)

    def resolve_prefix(self, prefix: str) -> None:
        for k in [k for k in self.found if k.startswith(prefix)]:
            self.resolve(k)

    # -- one step --

    def step(self, name: str, args, result) -> None:
        args = args if isinstance(args, dict) else {}
        res = result if isinstance(result, dict) else {}
        if name in JOB_TOOLS:
            self._job_step(args, res)
            return
        if name in CMD_TOOLS:
            cmd = str(args.get("cmd") or args.get("command") or "").strip()
            if not cmd:
                return
            base = str(args.get("cwd") or self.cwd or "")
            jid = str(res.get("id") or res.get("handle") or "")
            # ``bash`` is ONE shell for the whole chat: its folder and its
            # Python environment are whatever earlier calls left them.
            lasting = name == "bash"
            if res.get("running") is True or res.get("background") is True:
                if jid:
                    self.jobs[jid] = [cmd, base, lasting]
                return                       # nothing is proven until it ends
            if res.get("ok") is True:
                self.command(cmd, base, res, lasting=lasting)
            return
        if writes_files(name, args) and res.get("ok") is True:
            # The result's path is absolute: the argument is relative to the
            # chat's folder AT THE TIME, which may not be today's.
            given = str(res.get("path") or "")
            path = given if os.path.isabs(given) else str(
                args.get("path") or args.get("file") or given or "").strip()
            if path:
                self.file(path, created=res.get("created") is True)

    def _job_step(self, args: dict, res: dict) -> None:
        jid = str(res.get("id") or args.get("id") or args.get("pid") or "")
        if not jid or res.get("running") is not False:
            return
        cmd, base, lasting = self.jobs.pop(jid, (None, None, False))
        if cmd and res.get("ok") is True and res.get("code") == 0:
            self.command(cmd, base or self.cwd, res, lasting=bool(lasting))

    # -- files --

    def file(self, path: str, created: bool = False) -> None:
        full = path if os.path.isabs(path) else os.path.join(self.cwd or "", path)
        full = os.path.normpath(full)
        if self._temp_path(full):
            self._temp_entry(full, proven=created, what="temporary file")
            return
        prior = self.found.get(f"file:{full}") or {}
        self.add(_entry("file", f"file:{full}", "file written", full, "",
                        path=full, created=bool(prior.get("created")) or created))

    # -- commands --

    def command(self, cmd: str, base: str, res: dict, lasting: bool = False) -> None:
        """``lasting``: the command ran in the chat's one persistent shell. Its
        folder is then not known from the command alone (an earlier ``cd``
        still holds), so nothing that depends on WHERE it ran is read from it;
        and a bare ``pip`` there may be a virtualenv's."""
        parts = plain_parts(cmd, base)
        out = _output(res)
        for part in parts:
            part.env_switched = part.env_switched or lasting
            if lasting and os.path.basename(part.toks[0]) not in ("pip", "pip3", "apt-get",
                                                                  "apt", "brew", "gem",
                                                                  "cargo") \
                    and not os.path.basename(part.toks[0]).startswith("python"):
                continue
            head = os.path.basename(part.toks[0])
            try:
                if head in ("pip", "pip3", "uv") or (
                        head.startswith("python") and part.toks[1:3] == ["-m", "pip"]):
                    self._pip(part, out)
                elif head in ("npm", "yarn", "pnpm"):
                    self._node(head, part, out)
                elif head in ("apt-get", "apt", "brew", "gem", "cargo"):
                    self._system_pkg(head, part, out)
                elif head in ("docker", "docker-compose", "podman"):
                    self._docker(part, res, out)
                elif head == "git":
                    self._git(part, out)
                elif head == "rm" and not part.piped:
                    self._rm(part.toks[1:], part.here)
                self._temp(part, res, alone=len(parts) == 1)
            except Exception:  # noqa: BLE001 — one odd command never stops the replay
                continue

    def _pip(self, part: Part, out: str) -> None:
        toks, here = part.toks, part.here
        head = os.path.basename(toks[0])
        module = head.startswith("python")
        args = toks[3:] if module else toks[1:]
        exe = toks[0]
        if "/" in exe:                       # a named interpreter: the undo names it too
            exe = os.path.normpath(exe if os.path.isabs(exe)
                                   else os.path.join(here or "", os.path.expanduser(exe)))
        runner = f"{shlex.quote(exe)} -m pip" if module else shlex.quote(exe)
        # ``pip`` after an ``activate`` (in this command, or earlier in the
        # chat's persistent shell) or under sudo is another environment than
        # the one an undo run later would reach, and there the package may
        # predate the chat: no undo then.
        known_env = not part.env_switched and not part.sudo
        if head == "uv":
            if args[:1] == ["pip"]:
                args = args[1:]
                runner, known_env = "uv pip", not part.env_switched
            elif args[:1] == ["add"]:
                new = _pip_new(out)
                for p in _plain(args[1:]):
                    if _norm(_pkg_name(p)) in new:
                        self.add(_entry(
                            "package", f"pkg:uv:{here}:{_norm(_pkg_name(p))}",
                            f"uv dependency `{p}`", here,
                            self._at(here, f"uv remove {shlex.quote(_pkg_name(p))}")))
                return
            elif args[:1] == ["remove"]:
                for p in _plain(args[1:]):
                    self.resolve(f"pkg:uv:{here}:{_norm(_pkg_name(p))}")
                return
            else:
                return
        if not args:
            return
        verb, rest = args[0], args[1:]
        if verb == "uninstall":
            if not part.piped:
                for p in _plain(rest, ("-r", "--requirement")):
                    self.resolve(f"pkg:pip:{_norm(_pkg_name(p))}")
            return
        if verb != "install":
            return
        new = _pip_new(out)                  # pip's own list of what is new
        if not new:
            return
        asked = _plain(rest, ("-r", "--requirement", "-c", "--constraint", "-i",
                              "--index-url", "--extra-index-url", "-t", "--target",
                              "--prefix", "--root", "-f", "--find-links"))

        def record(name: str, shown: str) -> None:
            un = f"{runner} uninstall -y {shlex.quote(new[name])}"
            if runner == "uv pip":
                un = self._at(here, f"uv pip uninstall {shlex.quote(new[name])}")
            self.add(_entry("package", f"pkg:pip:{name}", shown, "",
                            un if known_env else "",
                            hint="" if known_env else _PIP_ENV_HINT))
        named = set()
        for p in asked:
            name = _norm(_pkg_name(p))
            if name in new:
                named.add(name)
                record(name, f"pip package `{p}`")
        req = _flag_value(rest, "-r", "--requirement")
        local = [p for p in asked if p in (".", "./") or p.startswith(("./", "/", "../"))]
        if req or local:
            # A requirements file or a local project: what pip newly installed
            # is the list, not the file (most of it may have been there already).
            for name in [n for n in new if n not in named][:40]:
                record(name, f"pip package `{new[name]}`" + (f" (from {req})" if req else ""))

    def _node(self, tool: str, part: Part, out: str) -> None:
        args, here = part.toks[1:], part.here
        if not args:
            return
        verb, rest = args[0], args[1:]
        glob = any(f in rest for f in ("-g", "--global")) or (
            tool == "yarn" and verb == "global")
        if tool == "yarn" and verb == "global" and rest:
            verb, rest = rest[0], rest[1:]
        names = _plain(rest)
        scope = "g:" if glob else here + ":"
        if verb in ("uninstall", "remove", "rm", "un", "r"):
            if not part.piped:
                for p in names:
                    self.resolve(f"pkg:{tool}:{scope}{self._node_name(p).lower()}")
            return
        made = {"npm": bool(_NPM_ADDED.search(out)),
                "yarn": "new dependenc" in out,
                "pnpm": bool(re.search(r"(?m)^\+ \S", out))}[tool]
        if verb in (("install", "i", "add") if tool == "npm" else ("add",)) and names:
            if not made:
                return                       # "up to date": it was there already
            # "added N packages" counts dependencies too. With ONE name asked for
            # and nothing changed or removed, that name is what was added; with
            # several, any of them may have been a dependency already.
            sure = len(names) == 1 and not _NPM_TOUCHED.search(out) and not part.env_switched
            for p in names:
                name = self._node_name(p)
                un = {"npm": "npm uninstall", "yarn": "yarn remove",
                      "pnpm": "pnpm remove"}[tool]
                if glob:
                    un = {"npm": "npm uninstall -g", "yarn": "yarn global remove",
                          "pnpm": "pnpm remove -g"}[tool]
                undo = f"{un} {shlex.quote(name)}"
                undo = undo if glob else self._at(here, undo)
                self.add(_entry(
                    "package", f"pkg:{tool}:{scope}{name.lower()}",
                    f"{tool} package `{p}`" + (" (global)" if glob else ""),
                    "" if glob else here, undo if sure else "",
                    hint="" if sure else f"`{un} {name}` only if it was not a dependency before"))
            return
        if tool == "npm" and verb in ("install", "i", "ci") and not names:
            path = os.path.join(here or "", "node_modules")
            added = _NPM_ADDED.search(out)
            if verb == "ci" or added:        # the tree may predate the chat: no rm
                what = "node_modules (npm ci)" if verb == "ci" else (
                    f"node_modules (npm install added {added.group(1)} packages)")
                self.add(_entry("package", f"pkg:node_modules:{path}", what, path, "",
                                path=path, hint="rebuilt by the package manager; "
                                                "remove it only if it was not there before"))

    @staticmethod
    def _node_name(spec: str) -> str:
        return "@" + _pkg_name(spec[1:]) if spec.startswith("@") else _pkg_name(spec)

    def _system_pkg(self, tool: str, part: Part, out: str) -> None:
        args = part.toks[1:]
        if not args:
            return
        verb, names = args[0], _plain(args[1:])
        pre = "sudo " if part.sudo else ""
        undo = {"apt-get": "apt-get remove -y", "apt": "apt-get remove -y",
                "brew": "brew uninstall", "gem": "gem uninstall",
                "cargo": "cargo uninstall"}[tool]
        fam = "apt" if tool.startswith("apt") else tool
        if verb in ("remove", "purge", "uninstall", "autoremove"):
            if not part.piped:
                for p in names:
                    self.resolve(f"pkg:{fam}:{_pkg_name(p).lower()}")
            return
        if verb != "install":
            return
        if fam == "apt":
            m = _APT_NEW.search(out)
            new = set(m.group(1).split()) if m else set()
        elif fam == "brew":
            new = set(names) if len(names) == 1 and ("Pouring" in out or "Cellar/" in out) \
                and "already installed" not in out else set()
        elif fam == "gem":
            new = {n for n in names if f"Successfully installed {n}-" in out}
        else:
            new = {n for n in names if f"Installed package `{n}" in out}
        for p in names:
            name = _pkg_name(p)
            if name in new or p in new:
                self.add(_entry("package", f"pkg:{fam}:{name.lower()}",
                                f"{fam} package `{p}`", "",
                                f"{pre}{undo} {shlex.quote(name)}"))

    def _docker(self, part: Part, res: dict, out: str) -> None:
        toks, here = part.toks, part.here
        exe = os.path.basename(toks[0])
        args = toks[1:]
        q = shlex.quote
        if exe == "docker-compose" or args[:1] == ["compose"]:
            rest = args if exe == "docker-compose" else args[1:]
            f = _flag_value(rest, "-f", "--file")
            proj = _flag_value(rest, "-p", "--project-name")
            verbs = _plain(rest, ("-f", "--file", "-p", "--project-name", "--env-file",
                                  "--profile", "--scale", "-t", "--timeout", "--pull",
                                  "--wait-timeout", "--exit-code-from", "--attach",
                                  "--no-attach"))
            key = f"docker:compose:{here}:{f}:{proj}"
            base = ("docker-compose" if exe == "docker-compose" else f"{exe} compose") \
                + (f" -f {q(f)}" if f else "") + (f" -p {q(proj)}" if proj else "")
            if verbs[:1] == ["up"]:
                services = verbs[1:]
                created = bool(_COMPOSE_CREATED.search(out))
                if not created and not _COMPOSE_STARTED.search(out):
                    return                   # everything was already up: not ours
                # ``down`` removes the WHOLE stack, so it is offered only when
                # this call made the whole stack: no service was picked, the
                # containers were created now, and none was running before.
                whole = created and not services and not _COMPOSE_WAS_UP.search(out)
                stop = base + " stop" + ("".join(f" {q(s)}" for s in services))
                self.add(_entry(
                    "docker", key,
                    "docker compose stack" + (f" `{proj or f}`" if proj or f else "")
                    + (f" (services: {', '.join(services)})" if services else ""),
                    # ``stop``, never ``down``: output saying "Created" does not
                    # prove no container, network or volume of this project
                    # existed before (stopped ones stay silent), and ``down``
                    # would remove those too.
                    here, self._at(here, stop) if whole else "",
                    cwd=here, project=proj,
                    hint=(f"remove it for good with: {self._at(here, base + ' down')} "
                          "(only if nothing in it predates this chat)") if whole
                    else f"it may predate this chat; stop what it started "
                         f"with: {self._at(here, stop)}"))
            elif verbs[:1] in (["down"], ["rm"]) and not part.piped:
                self.resolve(key)
            return
        if not args or part.piped:
            return                           # the rest is proven by the exit status
        verb, rest = args[0], args[1:]
        if verb == "container" and rest:
            verb, rest = rest[0], rest[1:]
        if verb == "run":
            detached = any(a in ("-d", "--detach") or (
                a.startswith("-") and not a.startswith("--") and "d" in a[1:]
                and a[1:].isalpha()) for a in rest)
            auto_rm = "--rm" in rest
            if auto_rm and not detached:
                return                       # it ran, ended and removed itself
            name = _flag_value(rest, "--name")
            if not name and detached:
                first = (str(res.get("stdout") or res.get("new_output") or "")
                         .strip().splitlines() or [""])[0].strip()
                name = first[:12] if _HEX.match(first) else ""
            if name:                         # a taken name makes ``run`` fail
                self.add(_entry(
                    "docker", f"docker:ctr:{name}",
                    f"docker container `{name}`" + ("" if detached else " (stopped)"),
                    "", f"{exe} {'stop' if auto_rm else 'rm -f'} {q(name)}",
                    auto_rm=auto_rm, name=name))
        elif verb in ("rm", "stop", "kill"):
            for n in _plain(rest):
                e = self.found.get(f"docker:ctr:{n}") or self.found.get(f"docker:ctr:{n[:12]}")
                if e and (verb == "rm" or e.get("auto_rm")):
                    self.resolve(e["key"])
                elif verb == "rm":
                    self.resolve(f"docker:ctr:{n}")
                    self.resolve(f"docker:ctr:{n[:12]}")

    def _git(self, part: Part, out: str) -> None:
        args, here = list(part.toks[1:]), part.here
        while args[:1] == ["-C"] and len(args) >= 2:
            here = os.path.normpath(os.path.join(here or "", args[1]))
            args = args[2:]
        if not args:
            return
        verb, rest = args[0], args[1:]
        if verb == "stash":                  # proven by git's own "Saved …" line
            sub = next((a for a in rest if not a.startswith("-")), "push")
            saved = _STASH_SAVED.search(out)
            if sub in ("push", "save") and saved:
                # The message git printed identifies THIS stash later on; the
                # undo is worked out against the live stash list.
                msg = saved.group(1).strip()[:200]
                self.add(_entry("git", f"git:stash:{here}:{msg}", "git stash entry", here,
                                "", cwd=here, stash_msg=msg))
            elif sub == "clear" and not part.piped:
                self.resolve_prefix(f"git:stash:{here}:")
            return                           # pop/drop: the live list is the judge
        if part.piped:
            return                           # the rest is proven by the exit status
        if verb in ("checkout", "switch"):
            # -b / -c fail on a branch that exists; -B / -C reset one and prove nothing.
            name = (_flag_value(rest, "-b") if verb == "checkout"
                    else _flag_value(rest, "-c", "--create"))
            if name:
                self._branch(name, here)
        elif verb == "branch":
            if any(a in ("-d", "-D", "--delete") for a in rest):
                for n in _plain(rest):
                    self.resolve(f"git:branch:{here}:{n}")
            elif not any(a.startswith("-") for a in rest) and 1 <= len(rest) <= 2:
                self._branch(rest[0], here)
        elif verb == "worktree" and rest:
            sub, more = rest[0], rest[1:]
            paths = _plain(more, ("-b", "-B"))
            if sub == "add" and paths:
                path = os.path.normpath(os.path.join(here or "", paths[0]))
                self.add(_entry(
                    "git", f"git:worktree:{path}", "git worktree", path,
                    self._at(here, f"git worktree remove {shlex.quote(path)}"),
                    path=path, cwd=here))
                name = _flag_value(more, "-b")
                if name:
                    self._branch(name, here)
            elif sub == "remove" and paths:
                self.resolve("git:worktree:" + os.path.normpath(
                    os.path.join(here or "", paths[0])))
        elif verb == "commit":
            # Pushing is the user's call, never a cleanup step.
            self.add(_entry("git", f"git:commits:{here}", "commits not pushed", here,
                            "", cwd=here))
        elif verb == "push":
            self.resolve(f"git:commits:{here}")

    def _branch(self, name: str, here: str) -> None:
        self.add(_entry("git", f"git:branch:{here}:{name}", f"git branch `{name}`", here,
                        # -d, not -D: git refuses when the branch holds commits
                        # that are merged nowhere, instead of throwing them away.
                        self._at(here, f"git branch -d {shlex.quote(name)}"),
                        branch=name, cwd=here))

    def _rm(self, args: list, here: str) -> None:
        for p in _plain(args):
            full = os.path.normpath(p if os.path.isabs(p) else os.path.join(here or "", p))
            for k in [k for k, e in self.found.items()
                      if e.get("path") and (e["path"] == full
                                            or str(e["path"]).startswith(full + "/"))]:
                self.resolve(k)

    def _temp_entry(self, full: str, *, proven: bool, what: str = "temporary path") -> None:
        if any(e.get("kind") == "temp" and full.startswith(str(e.get("path")) + "/")
               for e in self.found.values()):
            return                           # inside a temp dir already listed
        prior = self.found.get(f"temp:{full}") or {}
        proven = proven or bool(prior.get("proven"))
        self.add(_entry(
            "temp", f"temp:{full}",
            what if proven else f"{what} (written; it may predate this chat)", full,
            f"rm -rf {shlex.quote(full)}" if proven else "", path=full, proven=proven))

    def _temp(self, part: Part, res: dict, *, alone: bool) -> None:
        toks, here = part.toks, part.here
        head = os.path.basename(toks[0])
        proven: list = []
        written: list = []
        # The real mktemp only (a repo-local ./mktemp script could print any path).
        if (head == "mktemp" and toks[0] in ("mktemp", "/usr/bin/mktemp", "/bin/mktemp")
                and alone and not part.piped):
            # The whole command was one mktemp: its one output line is the path.
            lines = str(res.get("stdout") or "").strip().splitlines()
            if len(lines) == 1:
                proven.append(lines[0].strip())
        elif head == "mkdir" and not part.piped:
            parents = any(a == "--parents" or (a.startswith("-") and not a.startswith("--")
                                               and "p" in a) for a in toks[1:])
            (written if parents else proven).extend(_plain(toks[1:], ("-m", "--mode")))
        elif head == "touch" and not part.piped:
            written += _plain(toks[1:])
        elif head == "git" and toks[1:2] == ["clone"] and not part.piped:
            # clone refuses a non-empty destination; with no destination given
            # the last word is the SOURCE, which is never ours to remove.
            words = _plain(toks[2:], ("-b", "--branch", "--depth", "-o", "--origin", "-c",
                                      "--config", "--reference", "--template", "-j",
                                      "--jobs", "--shallow-since", "--filter", "-u",
                                      "--upload-pack", "--separate-git-dir"))
            if len(words) == 2:
                proven.append(words[1])
        written += [target for op, target in part.redirects if op == ">"]
        for cands, ok in ((proven, True), (written, False)):
            for c in cands:
                if not c or c.startswith("&"):
                    continue
                full = os.path.normpath(c if os.path.isabs(c) else os.path.join(here or "", c))
                if self._temp_path(full):
                    self._temp_entry(full, proven=ok)


def replay(steps, cwd: str = "") -> dict:
    """``{key: entry}`` for what ``steps`` (tool steps, oldest first) left
    behind. Never raises."""
    r = Replay(cwd)
    for s in steps or []:
        try:
            if isinstance(s, dict) and s.get("type") == "tool":
                r.step(str(s.get("name") or ""), s.get("args"), s.get("result"))
        except Exception:  # noqa: BLE001
            continue
    return r.found


__all__ = ["CMD_TOOLS", "JOB_TOOLS", "Part", "Replay", "is_temp_path", "plain_parts",
           "redact", "replay", "temp_roots"]
