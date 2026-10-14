"""Which repositories a chat WORKS in and which it only READS — recorded once,
followed for the rest of the chat.

A task over several repositories (port repo A to another language in repo B;
read a spec in C) went wrong in one way again and again: half way through, the
agent picked up the source repository and started changing it instead of the
destination. Nothing in the chat said which one was which, so after a long
run, a condensed context or a new turn, it was a guess.

Now the chat records it, on its task board:

    plan_progress {"slug": "repos", "work_in": ["/path/B"], "read_only": ["/path/A"]}

The record is per chat (a side task uses its parent's), kept in
``chat-repo-roles.json`` in the config dir so it survives a restart, shown in
the system prompt of every turn, and enforced before every tool call:

* a write (file tools, a shell command's write targets, a git commit / push /
  branch switch) into a READ-ONLY folder is refused, naming the folder to work
  in instead;
* once roles are recorded, a write into another git repository that is in
  neither list is refused;
* while two or more folders are in play (the chat's repository, the
  repositories the user named, the ones it already changed, a destination the
  user typed) and no roles are recorded, the first write into a repository is
  refused until they are;
* after each command that could write (shell, python, runners), the read-only
  folders are measured against a snapshot taken before it (see "the net"
  below): a change stops the turn and lists the paths. Nothing is undone.

Making a read-only folder writable needs the user, in one clause: "allow edits
in calc-java" (:func:`user_allows`). The harness applies it itself
(:func:`allow_from_user`), and a turn refused a change ends with that question
(:func:`final_note`). Making a folder read-only, or adding folders, is free.
``AIFORGE_CHAT_REPO_ROLES=0`` turns all of it off; ``AIFORGE_CHAT_REPO_ROLES_NET=0``
only the before/after measurement.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
WORK, READ = "work", "read"
SLUG = "repos"
#: plan_progress keys that carry roles.
WORK_KEYS = ("work_in", "work", "destination", "destinations")
READ_KEYS = ("read_only", "read", "source", "sources", "reference")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_REPO_ROLES", "1").strip().lower() \
        not in ("0", "false", "no", "off")


# ── store ────────────────────────────────────────────────────────────────────

def _path() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(str(config_dir()), "chat-repo-roles.json")


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
    except Exception as exc:  # noqa: BLE001
        log.debug("repo roles not saved: %s", exc)


_OWNERS: dict = {}


def owner(session_id):
    """The chat whose record ``session_id`` uses: a side task its parent's.
    Cached: a chat's parent never changes."""
    if session_id is None:
        return None
    key = str(session_id)
    if key not in _OWNERS:
        got = session_id
        try:
            from aiforge_core.runtime import chat_store
            sess = chat_store.get_session(int(session_id)) or {}
            if sess.get("parent_id"):
                got = sess["parent_id"]
        except Exception:  # noqa: BLE001 — not cached: asked again next time
            return session_id
        if len(_OWNERS) > 4096:
            _OWNERS.clear()
        _OWNERS[key] = got
    return _OWNERS[key]


def is_child(session_id) -> bool:
    """A side task (its own words are its parent agent's, not the user's)."""
    return session_id is not None and str(owner(session_id)) != str(session_id)


def roles(session_id) -> dict:
    """``{"work": [folders], "read": [folders]}`` (both empty: none recorded)."""
    sid = owner(session_id)
    out = {WORK: [], READ: []}
    if sid is None:
        return out
    with _LOCK:
        got = _load().get(str(sid))
    if isinstance(got, dict):
        for k in (WORK, READ):
            out[k] = [p for p in got.get(k) or [] if isinstance(p, str) and p]
    return out


def declared(session_id) -> bool:
    r = roles(session_id)
    return bool(r[WORK] or r[READ])


def forget(session_id=None) -> None:
    if session_id is None:
        _OWNERS.clear()               # ids restart at 1 after delete-all
    else:
        _OWNERS.pop(str(session_id), None)
    with _LOCK:
        data = {} if session_id is None else _load()
        if session_id is None or data.pop(str(session_id), None) is not None:
            _save(data)


# ── folders ──────────────────────────────────────────────────────────────────

def _real(path: str, base: "str | None") -> str:
    p = os.path.expanduser(str(path).strip())
    if not os.path.isabs(p):
        p = os.path.join(base or os.getcwd(), p)
    return os.path.realpath(p)


def _main_path(real: str) -> str:
    """``real`` seen from the project: a path inside a chat's (or any linked)
    worktree maps to the same path in its main repository, so a role on
    ``/proj/src`` covers ``/proj/.aiforge-worktrees/chat-1/work/src`` too."""
    from aiforge_core.runtime import git_target
    from aiforge_core.runtime.chat_worktree import main_repo_of
    top = git_target.toplevel(real)
    main = main_repo_of(top) if top else None
    if not top or not main:
        return real
    rel = os.path.relpath(real, top)
    return os.path.realpath(main) if rel == "." else os.path.join(os.path.realpath(main), rel)


def folder_of(path: str, base: "str | None" = None) -> str:
    """The folder a role names, seen from the project: a folder as given
    (``/proj/src`` stays itself: roles can split one repository), a file → the
    repository it is in, a path not made yet → itself."""
    from aiforge_core.runtime import git_target
    real = _real(path, base)
    if os.path.isdir(real):
        return _main_path(real)
    if os.path.exists(real):
        top = git_target.toplevel(real)
        return _main_path(top) if top else os.path.dirname(real)
    return real


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _candidates(path: str, base: "str | None") -> set:
    real = _real(path, base)
    return {real, _main_path(real)}


def role_of(path: str, session_id, base: "str | None" = None) -> "tuple[str, str] | None":
    """``(role, folder)`` for a path: the most specific recorded folder that
    holds it, else None."""
    r = roles(session_id)
    cands = _candidates(path, base)
    best = None
    for role in (WORK, READ):
        for folder in r[role]:
            if any(_under(c, folder) for c in cands) and (
                    best is None or len(folder) > len(best[1])):
                best = (role, folder)
    return best


def _repo_role(top: str, session_id) -> str:
    """For a git command run in repository ``top``: READ when the repository
    is (inside) a read-only folder, WORK when a work folder is in it or holds
    it, "" when it has no role."""
    r = roles(session_id)
    main = _main_path(top)
    # A work folder inside it (``/proj/rust`` of a read-only ``/proj``): git
    # there is work; what it changes in the tree is checked folder by folder.
    if any(_under(f, main) for f in r[WORK]):
        return WORK
    hit = role_of(main, session_id)
    return hit[0] if hit else ""


# ── recording ────────────────────────────────────────────────────────────────

def has_roles(args) -> bool:
    return isinstance(args, dict) and any(args.get(k) for k in WORK_KEYS + READ_KEYS)


def _listed(args, keys) -> list:
    out = []
    for k in keys:
        v = args.get(k)
        if isinstance(v, str):
            v = [s for s in v.replace(";", ",").split(",")]
        if isinstance(v, list):
            out += [str(s).strip() for s in v if str(s).strip()]
    return out


def _named(folder: str, said: str) -> bool:
    from aiforge_core.runtime.git_target import folder_named
    return folder_named(folder, said)


def _resolve(raw: str, base: "str | None", known) -> "str | None":
    """The folder a role entry means. An absolute path is itself (it may not
    exist yet: a destination to create). A relative one or a bare name
    (``rust-port``) must exist: under the chat's folder, next to its project,
    or as one of the folders in play — never by walking up to whatever
    repository holds a missing path (that recorded the destination itself)."""
    p = os.path.expanduser(str(raw).strip()).rstrip(os.sep) or os.sep
    if os.path.isabs(p):
        return folder_of(p)
    found: list = [f for f in known
                   if os.path.basename(f) == p or f.endswith(os.sep + p)]
    roots = [base] if base else []
    if base:
        project = folder_of(base)
        roots += [project, os.path.dirname(project)]
    for root in roots:
        cand = os.path.join(root, p)
        if os.path.exists(cand):
            found.append(folder_of(cand))
    found = list(dict.fromkeys(found))
    if len(found) > 1:
        raise ValueError(f"'{raw}' could mean " + " or ".join(found))
    return found[0] if found else None


def record(session_id, args: dict, base: "str | None", said: str = "",
           user_roots=()) -> dict:
    """Apply the roles in a plan_progress call. Returns the tool result."""
    sid = owner(session_id)
    if sid is None:
        return {"ok": False, "error": "repo roles are kept per chat; this run has none"}
    known = in_play(session_id, base, user_roots, said)
    work, read, missing = [], [], []
    for keys, into in ((WORK_KEYS, work), (READ_KEYS, read)):
        for raw in _listed(args, keys):
            try:
                got = _resolve(raw, base, known)
            except ValueError as exc:
                return {"ok": False, "error": f"{exc}. Give the FULL path."}
            if got is None:
                missing.append(raw)
            elif got not in into:
                into.append(got)
    if missing:
        return {"ok": False, "error": "not found: " + ", ".join(missing) + ". Give each "
                "folder as its FULL path (a destination that does not exist yet too)."}
    both = sorted(set(work) & set(read))
    notes = []
    if both:
        # Listed in both: keep what is recorded for it; say so.
        work = [f for f in work if f not in both]
        read = [f for f in read if f not in both]
        notes.append("ignored (listed as both work and read only): " + ", ".join(both))
    with _LOCK:
        data = _load()
        have = data.get(str(sid)) if isinstance(data.get(str(sid)), dict) else {}
        cur = {WORK: list(have.get(WORK) or []), READ: list(have.get(READ) or [])}
        # Making a folder writable that is (inside) a read-only one is a move,
        # as is turning a work folder read-only.
        moved = [f for f in work if any(_under(f, g) for g in cur[READ])] \
            + [f for f in read if f in cur[WORK]]
        # A side task's words are its parent agent's: it never moves a role.
        refused = [f for f in moved if is_child(session_id) or not (
            user_allows(f, said) if f in work else _named(f, said))]
        if refused:
            # The rest of the call still applies; these keep their role.
            work = [f for f in work if f not in refused]
            read = [f for f in read if f not in refused]
            notes.append("NOT changed (the user did not name them in this turn; roles "
                         "change only when the user says so): " + ", ".join(refused))
        for f in work:
            cur[READ] = [g for g in cur[READ] if g != f]
            if f not in cur[WORK]:
                cur[WORK].append(f)
        for f in read:
            if f in cur[WORK]:
                cur[WORK].remove(f)
            if f not in cur[READ]:
                cur[READ].append(f)
        changed = cur != {WORK: list(have.get(WORK) or []), READ: list(have.get(READ) or [])}
        if changed:
            data[str(sid)] = cur
            _save(data)
    out = {"ok": bool(changed or not notes), "repos": cur, "changed": changed,
           "note": ("Recorded for the rest of this chat: " if changed
                    else "Already recorded, unchanged: ") + summary(cur)}
    if notes:
        out["error" if not changed else "warning"] = "; ".join(notes)
    return out


_ALLOW = (r"\b(?:allow|permit|unlock|enable|you\s+(?:may|can)|go\s+ahead\s+and|ok(?:ay)?\s+to)"
          r"\b[^.;\n]{0,30}?\b(?:edit\w*|chang\w*|modif\w*|writ\w*|commit\w*|updat\w*)\b")
_NEG = re.compile(r"\b(?:don'?t|do\s+not|never|not|no)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


def user_allows(folder: str, said: str) -> bool:
    """The user's words say, in ONE clause, that ``folder`` may be changed:
    "allow edits in calc-java", "you may change /code/a". Naming the folder
    elsewhere in the message ("port it from calc-java; you may update the
    tests") is not it, nor is a path inside it, nor "don't allow edits in …"."""
    if not folder or not said:
        return False
    folder = folder.rstrip(os.sep)
    names = [re.escape(folder), re.escape(os.path.basename(folder))]
    pat = re.compile(_ALLOW + r"[^.;\n]{0,25}?(?<![\w./-])`?(?:" + "|".join(names)
                     + r")`?(?![\w/-]|\.\w)", re.IGNORECASE)
    for m in pat.finditer(said):
        if not _NEG.search(said[max(0, m.start() - 20):m.start()]):
            return True
    return False


def allow_from_user(session_id, said: str) -> list:
    """The user's words this turn allow changes in a read-only folder: move it
    to the work list. Done by the harness, not left to the model. Returns the
    folders moved."""
    sid = owner(session_id)
    if sid is None or not said or is_child(session_id):
        return []
    r = roles(session_id)
    moved = [f for f in r[READ] if user_allows(f, said)]
    if not moved:
        return []
    with _LOCK:
        data = _load()
        cur = data.get(str(sid)) if isinstance(data.get(str(sid)), dict) else {}
        cur = {WORK: list(cur.get(WORK) or []), READ: list(cur.get(READ) or [])}
        for f in moved:
            cur[READ] = [g for g in cur[READ] if g != f]
            if f not in cur[WORK]:
                cur[WORK].append(f)
        data[str(sid)] = cur
        _save(data)
    return moved


def summary(r: dict) -> str:
    parts = []
    if r.get(WORK):
        parts.append("work in " + ", ".join(r[WORK]))
    if r.get(READ):
        parts.append("read only " + ", ".join(r[READ]))
    return "; ".join(parts) or "nothing"


# ── what is in play ──────────────────────────────────────────────────────────

def _typed_folders(said: str) -> list:
    """Folders the user TYPED this turn that are not repositories: a folder
    that exists, or a path not made yet whose parent exists (a destination:
    "port this to /code/new-rs"). A file the user pasted (a log, a spec) is
    not a folder the chat works on."""
    from aiforge_core.runtime.scope_guard import _USER_PATH_RE
    out = []
    for raw in _USER_PATH_RE.findall(said or ""):
        p = os.path.realpath(os.path.expanduser(raw.rstrip(".:!?*_`")))
        if os.path.isdir(p) or (not os.path.exists(p)
                                and os.path.isdir(os.path.dirname(p))
                                and "." not in os.path.basename(p)):
            out.append(p)
    return out


def _skip_folder(real: str) -> bool:
    """Never a folder of the work: home, /, temp, AIForge's own config."""
    from aiforge_core.config.paths import config_dir
    from aiforge_core.runtime import git_target, shell_writes as sw
    home = os.path.realpath(os.path.expanduser("~"))
    cfg = os.path.realpath(str(config_dir()))
    return (real in (home, os.sep) or _under(home, real) or sw._is_temp(real)
            or _under(real, cfg) or git_target.is_scratch(real))


def in_play(session_id, base: "str | None", user_roots=(), said: str = "") -> list:
    """The folders this chat involves: its own folder's repository, the
    repositories the user named, the ones it already changed, and folders the
    user typed this turn that are not repositories (a destination to create).
    A repository and folders inside it count once."""
    from aiforge_core.runtime import git_target
    found: list = []

    def add(f: str) -> None:
        if any(_under(f, g) or _under(g, f) for g in found):
            return                    # the same tree: one folder
        found.append(f)

    for c in [base] + list(git_target.pins(owner(session_id))) + list(user_roots or ()):
        if not c or _skip_folder(os.path.realpath(c)):
            continue
        top = git_target.toplevel(c)
        if top:
            add(_main_path(top))
    for real in _typed_folders(said):
        if not _skip_folder(real) and not git_target.toplevel(real):
            add(real)
    return found


# ── the check ────────────────────────────────────────────────────────────────

#: Git subcommands that change nothing in the working tree or the history.
_GIT_READS = frozenset({"status", "log", "diff", "show", "blame", "grep", "ls-files",
                        "ls-tree", "rev-parse", "describe", "shortlog", "cat-file",
                        "ls-remote", "rev-list", "fetch", "help", "version",
                        "for-each-ref", "show-ref", "reflog", "remote",
                        "name-rev", "merge-base", "count-objects",
                        # makes a NEW repository elsewhere (its target is a
                        # shell write target); the one it runs in is not changed
                        "clone", "archive", "bundle"})
#: Git subcommands that rewrite files in the working tree (all of it when no
#: path is given): checked against the folders, not only the repository.
_TREE_WRITES = frozenset({"checkout", "restore", "rm", "mv", "clean", "reset",
                          "stash", "apply", "am"})
#: Tools that work on the chat's folder when no path is given.
_DEFAULT_DOT = frozenset({"rename_symbol", "format"})


def _git_reads(c) -> bool:
    if c.sub in _GIT_READS:
        return True
    ops = [a for a in c.args if not a.startswith("-")]
    if c.sub == "config":             # ``config core.fsmonitor <cmd>`` runs later
        return bool({"--get", "--get-all", "--get-regexp", "-l", "--list"} & set(c.args))
    if c.sub == "stash":              # a bare ``git stash`` pushes
        return bool(ops) and ops[0] in ("list", "show")
    if c.sub == "branch":
        makes = {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move",
                 "--copy", "--force", "-u", "--set-upstream-to", "--unset-upstream"}
        return not (makes & set(c.args)) and (
            not ops or bool({"-l", "--list"} & set(c.args)))
    if c.sub == "tag":
        return not ops or bool({"-l", "--list"} & set(c.args))
    return bool(set(c.args) & {"--continue", "--abort", "--skip", "--quit"}) \
        and c.sub in ("rebase", "merge", "cherry-pick", "revert", "am")


def _targets(name: str, args: dict, cwd: str) -> "tuple[list, list]":
    """``(paths a call writes, repositories its git commands change)``. File
    tools resolve against the chat's folder, commands against run_command's."""
    from aiforge_core.runtime import git_target, shell_writes as sw
    from aiforge_core.runtime.tools.mutating import writes_files
    if writes_files(name, args) or name in _DEFAULT_DOT:
        from aiforge_core.runtime.chat_agent._turn._progress import _full, _named_paths
        paths = _named_paths(args, None) or (["."] if name in _DEFAULT_DOT else [])
        return [p for p in (_full(p, cwd) for p in paths)
                if not git_target.is_scratch(p)], []
    if name not in sw.SHELL_TOOLS:
        return [], []
    cmd = sw.command_of(args)
    if not cmd:
        return [], []
    base = os.path.realpath(git_target.command_base(cwd))
    repos = []
    paths = list(git_target.edited_paths(cmd, base))
    if "git" in cmd:
        for c in git_target.calls(cmd, base):
            if _git_reads(c):
                continue
            if not c.folder:
                repos.append(None)    # cannot tell where: decided by the check
                continue
            top = git_target.toplevel(c.folder)
            if top and top not in repos:
                repos.append(top)
            if top and c.sub in _TREE_WRITES:
                # What it changes in the tree: its paths, or all of it.
                ops = [a for a in c.args if not a.startswith("-")]
                if c.sub == "stash" and ops and ops[0] in ("push", "save"):
                    ops = ops[1:]
                if c.sub in ("checkout", "reset") and "--" not in c.args:
                    ops = ops[1:] if len(ops) > 1 else []
                if c.sub in ("apply", "am"):
                    ops = []          # the files are named inside the patch
                if ops:
                    paths += [os.path.join(c.folder, o) for o in ops]
                else:
                    paths.append(top + os.sep)   # the whole tree (see _check)
    return paths, repos


_PY_WRITES = re.compile(r"""open\([^)]*['"][wax+]|write_text|write_bytes|shutil\.|"""
                        r"""os\.(?:remove|unlink|rename|replace|makedirs|mkdir|rmdir)|"""
                        r"""subprocess|os\.system|\.unlink\(|\.rename\(|\.mkdir\(""")


def _python_writes_into(code: str, r: dict) -> "str | None":
    """A Python cell that names a read-only folder and writes something."""
    if not code or not _PY_WRITES.search(code):
        return None
    for folder in r[READ]:
        if folder in code:
            return folder
    return None


def check(session_id, name: str, args: dict, cwd: "str | None",
          user_roots=(), said: str = "") -> "dict | None":
    """The refusal for a call that writes where this chat must not, or None.
    ``cwd``: the chat's folder."""
    if session_id is None or not enabled() or not cwd:
        return None
    try:
        return _check(session_id, name, args or {}, os.path.realpath(cwd), user_roots,
                      said)
    except Exception as exc:  # noqa: BLE001 — a reader bug never blocks a call
        log.debug("repo roles check skipped: %s", exc)
        return None


def _ask_to_allow(folder: str) -> str:
    return (f"If the user's request needs {folder} changed, do the rest of the work, then "
            f"end with this question in FINAL: \"{folder} is recorded as read-only for "
            f"this chat — may I change it? Reply 'allow edits in "
            f"{os.path.basename(folder)}'.\" Do not try another way to write there.")


def _work_list(r: dict) -> str:
    return ", ".join(r[WORK]) or "the folder you work in"


def _check(session_id, name, args, base, user_roots, said=""):
    from aiforge_core.runtime import git_target
    r = roles(session_id)
    if name == "execute_ipython_cell" and (r[WORK] or r[READ]):
        hit = _python_writes_into(str(args.get("code") or ""), r)
        if hit:
            return _refusal("read_only", (
                f"Refused: this Python cell writes and names {hit}, which this chat only "
                f"READS. Make changes in {_work_list(r)}. " + _ask_to_allow(hit)), hit)
        return None
    paths, repos = _targets(name, args, base)
    if not (paths or repos):
        return None
    if not (r[WORK] or r[READ]):
        repos = [t for t in repos if t]
        play = in_play(session_id, base, user_roots, said)
        if len(play) < 2:
            return None
        if repos or any(git_target.toplevel(t) for t in paths):
            return _refusal("roles_missing", (
                "Not run yet: this chat involves more than one folder ("
                + ", ".join(play) + ") and has not recorded which it CHANGES and which "
                "it only READS. Record it now from the user's request — one call: "
                "plan_progress {\"slug\": \"repos\", \"work_in\": [<full paths to "
                "change>], \"read_only\": [<full paths of sources / references>]} — "
                "then repeat this step. Ask the user only if the request does not say."))
        return None
    if None in repos:
        return _refusal("unknown_repo", (
            "Refused: cannot tell which repository this git command changes (a `cd` "
            "or --work-tree whose folder is not known before it runs). Name it: "
            f"`git -C <full path> …`. This chat works in {_work_list(r)}."))
    for top in repos:
        role = _repo_role(top, session_id)
        if role == READ:
            return _refusal("read_only", (
                f"Refused: this git command changes {top}, which this chat only READS "
                f"(source / reference). Run git for the work in {_work_list(r)}. "
                + _ask_to_allow(_main_path(top))), _main_path(top))
        if not role:
            return _refusal("not_a_work_repo", (
                f"Refused: this git command changes {top}, which is not one of the "
                f"folders this chat works in ({summary(r)}). If the user asked for it, "
                "add it with plan_progress {\"slug\": \"repos\", \"work_in\": [...]}."))
    for t in paths:
        if t.endswith(os.sep) and len(t) > 1:
            # A git command over the whole tree: refused when a read-only
            # folder is part of that tree.
            main = _main_path(t.rstrip(os.sep))
            inside = [f for f in r[READ] if _under(f, main)]
            if inside:
                return _refusal("read_only", (
                    f"Refused: this git command changes the whole tree of {main}, and "
                    f"{inside[0]} in it is READ ONLY for this chat. Name the paths it "
                    f"should touch (in {_work_list(r)}). " + _ask_to_allow(inside[0])),
                    inside[0])
            t = t.rstrip(os.sep)
        hit = role_of(t, session_id, base)
        if hit and hit[0] == READ:
            return _refusal("read_only", (
                f"Refused: {t} is in {hit[1]}, which this chat only READS (source / "
                f"reference). Do not change it. Make the change in {_work_list(r)}. "
                + _ask_to_allow(hit[1])), hit[1])
        if hit is None and git_target.toplevel(t):
            return _refusal("not_a_work_repo", (
                f"Refused: {t} is not in a folder this chat works in ({summary(r)}). "
                f"Work only in {_work_list(r)}. If the user asked for this folder, add "
                "it with plan_progress {\"slug\": \"repos\", \"work_in\": [...]}."))
    return None


# ── the net: what a command really changed ──────────────────────────────────
#
# Reading a shell command cannot be complete (``xargs``, ``$VAR``, ``node -e``,
# ``env -C``, a path built inside a script …). So every command that could
# write is bracketed: the read-only folders are measured before and after
# (team_repo_net's snapshot: HEAD, branch, ``git status`` and a content hash of
# each dirty path; a folder listing for a folder that is not a repository). A
# change in one is reported and the turn stops for the user. Nothing is undone:
# an automatic undo cannot tell the command's change from the user's own edit.

#: Tools that can change files in ways the pre-check cannot read.
_NET_TOOLS = frozenset({"execute_ipython_cell", "workflow_run", "run_tests",
                        "delegate", "format", "rename_symbol", "mcp"})


def _net_on() -> bool:
    return os.environ.get("AIFORGE_CHAT_REPO_ROLES_NET", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _measure(folder: str):
    """``(kind, root, state)`` for a read-only folder; None when it cannot be
    read (then it is not watched for that call)."""
    from aiforge_core.runtime import git_target, team_repo_net as net
    top = git_target.toplevel(folder) if os.path.isdir(folder) else None
    try:
        if top:
            return ("git", top, net.snapshot(top))
        if os.path.isdir(folder):
            return ("dir", folder, net._dir_mark(folder))
    except Exception as exc:  # noqa: BLE001 — SnapError, OS errors
        log.debug("read-only folder not measured (%s): %s", folder, exc)
    return None


def net_begin(session_id, name: str, cwd: "str | None" = None):
    """Measure the read-only folders before a call that could change them."""
    from aiforge_core.runtime import shell_writes as sw
    if session_id is None or not enabled() or not _net_on() \
            or (name not in sw.SHELL_TOOLS and name not in _NET_TOOLS):
        return None
    try:
        r = roles(session_id)
        folders = list(r[READ])
        if not folders:
            return None
        before = {f: _measure(f) for f in folders}
        return {"session": session_id, "before": before}
    except Exception as exc:  # noqa: BLE001
        log.debug("repo roles net skipped: %s", exc)
        return None


def _changed(folder: str, was, now) -> list:
    from aiforge_core.runtime import team_repo_net as net
    if was is None or now is None or was[0] != now[0]:
        return []
    if was[0] == "dir":
        return [folder] if was[2] != now[2] else []
    paths, ref_moved = net.diff(was[2], now[2])
    root = was[1]
    out = []
    for p in paths:
        full = os.path.join(root, p) if not p.startswith("(") else root
        # A read-only SUBFOLDER of a repository: only changes inside it count.
        if p.startswith("(") or _under(_main_path(os.path.realpath(full)), folder) \
                or _under(os.path.realpath(full), folder):
            out.append(full)
    if ref_moved and _main_path(root) == folder:
        out.append(f"{root} (HEAD / branch moved)")
    return out


def net_end(handle, result):
    """``(result, note)`` after the call: the call's result replaced by a
    stop when it changed a read-only folder. Never raises."""
    if not handle:
        return result, ""
    try:
        hits: list = []
        for folder, was in handle["before"].items():
            hits += _changed(folder, was, _measure(folder))
        if not hits:
            return result, ""
        shown = ", ".join(hits[:6]) + (f" (+{len(hits) - 6} more)" if len(hits) > 6 else "")
        note = ("⛔ A command changed a READ-ONLY folder of this chat: " + shown
                + ". Nothing was undone. Check it (`git status` / `git diff` there) and "
                "say how to go on; to let the chat change it, reply 'allow edits in "
                + os.path.basename(next(iter(handle["before"]))) + "'.")
        return ({"ok": False, "error": "changed_read_only", "stop": True,
                 "changed": hits, "hint": (
                     "STOP: this call changed a folder this chat only READS (" + shown
                     + "). Do not run anything else and do not try to undo it; the "
                     "user has been told and decides.")}, note)
    except Exception as exc:  # noqa: BLE001
        log.debug("repo roles net check failed: %s", exc)
        return result, ""


def refused_folder(result) -> "str | None":
    """The read-only folder a refusal was about, for the turn's final note."""
    if isinstance(result, dict) and result.get("blocked") == "repo_roles" \
            and result.get("reason") == "read_only":
        return result.get("folder") or None
    return None


def final_note(folders, session_id, text: str) -> str:
    """The question under a final answer when the turn was refused a change in
    a read-only folder that is still read-only ("" when none, or the answer
    already asks it)."""
    if not folders or session_id is None:
        return ""
    still = [f for f in dict.fromkeys(folders) if f in roles(session_id)[READ]]
    names = [os.path.basename(f) for f in still
             if f"allow edits in {os.path.basename(f)}" not in (text or "")]
    if not names:
        return ""
    return ("\n\n🔒 Not changed: " + ", ".join(f"`{n}`" for n in names)
            + (" is" if len(names) == 1 else " are") + " read-only for this chat. To let "
            "me change " + ("it" if len(names) == 1 else "them") + ", reply "
            + " / ".join(f"'allow edits in {n}'" for n in names) + ".")


def _refusal(kind: str, text: str, folder: str = "") -> dict:
    if folder:
        return {"ok": False, "blocked": "repo_roles", "reason": kind, "error": text,
                "folder": folder}
    return {"ok": False, "blocked": "repo_roles", "reason": kind, "error": text}


def prompt_note(session_id, base: "str | None" = None, user_roots=(), said: str = "") -> str:
    """The roles, for every turn's context ("" when none and one repository)."""
    if session_id is None or not enabled():
        return ""
    try:
        r = roles(session_id)
        if r[WORK] or r[READ]:
            lines = ["REPOSITORIES IN THIS CHAT (recorded; follow them every step):"]
            if r[WORK]:
                lines.append("- WORK IN (change files, build, commit here): "
                             + ", ".join(r[WORK]))
            if r[READ]:
                lines.append("- READ ONLY (source / reference: read it, never edit, "
                             "never commit there): " + ", ".join(r[READ]))
            lines.append("Each run_command starts in the chat's folder; use absolute "
                         "paths or `git -C <folder>` for the folder you mean.")
            return "\n".join(lines)
        play = in_play(session_id, base, user_roots, said)
        if len(play) >= 2:
            return ("SEVERAL REPOSITORIES: " + ", ".join(play) + ". Before the first "
                    "change, record from the user's request which you WORK IN and which "
                    "are READ ONLY (source / reference): plan_progress {\"slug\": "
                    "\"repos\", \"work_in\": [...], \"read_only\": [...]}. It is kept for "
                    "the whole chat.")
    except Exception as exc:  # noqa: BLE001
        log.debug("repo roles note skipped: %s", exc)
    return ""


__all__ = ["READ", "SLUG", "WORK", "check", "declared", "enabled", "folder_of",
           "forget", "has_roles", "in_play", "owner", "prompt_note", "record",
           "role_of", "roles", "summary"]
