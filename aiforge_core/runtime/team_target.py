"""Where a TEAM / build-pipeline turn works: the folder the user named.

Chat (simple) mode already lets the agent write in a folder the user typed
(scope_guard.user_named_roots → the workspace jail's extra roots). The team
pipeline did not look: "fix money.py in /home/me/proj so tests/ pass" planned,
built, merged and "tested" inside the session scratch workspace — even creating
a literal ``home/me/proj/`` subfolder there — and reported success while the
user's repo and tests were never touched.

:func:`resolve_team_target` reads the paths in the user's OWN words (same
regex and consent rule as the chat jail: a path the user typed is consent to
work there) and returns the directory the pipeline should use as its cwd — the
git root when the folder is inside a repo. A named folder that does not exist
is never invented: the caller asks the user instead.

:func:`anchor_subtask_paths` is the second half: a planner that copies the
user's absolute path into a subtask's file path must not turn it into a
path-shaped subfolder of the workspace.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# A trailing token like ".py" marks a FILE (to edit or create); anything else
# that does not exist is a folder the user expects to already be there.
_FILE_EXT_MAX = 8


@dataclass
class TeamTarget:
    """``cwd`` is the directory to run in (the input cwd when nothing was
    named). ``named`` is the folder the user named (may sit below ``cwd`` when
    ``cwd`` is its git root). ``missing`` lists named paths that do not exist
    — the caller must ask, not build. ``others`` are further distinct roots the
    user named (only the first becomes the target)."""
    cwd: str
    named: str = ""
    missing: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    # The named folder is not inside a git repo: the team needs one, and the
    # caller must ASK before initialising it (never ``git init`` silently).
    init_needed: bool = False
    # Named folders that can never be a build target (``~``, ``/``, ``/tmp``,
    # ``~/Documents`` …).
    ignored: list[str] = field(default_factory=list)

    @property
    def retargeted(self) -> bool:
        return bool(self.named)


# Pasted material is not an instruction: a fenced block, a quoted line, a
# traceback frame or a shell-prompt line naming /opt/app does not ask for work
# there.
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
# An indented line is pasted code only when it is not a list item: a nested
# bullet ("    - fix /Users/me/proj/app.py") is the user's own instruction.
_PASTED_LINE_RE = re.compile(
    r"^(?:\s*>|\s*\$ |(?:\s{4,}|\t)(?![ \t]*(?:[-*+]|\d{1,3}[.)])\s)|"
    r"\s*File \"|\s*at \S|\s*\[?\d{4}-\d\d-\d\d|"
    r"\s*\d\d:\d\d:\d\d|\s*(?:INFO|DEBUG|WARN(?:ING)?|ERROR|TRACE)\b|\s*Traceback)")
# System trees are only a target when the user's own phrase directs work
# there ("in /opt/app", "fix /usr/local/bin/tool"); device/kernel trees never.
_NEVER_PREFIXES = ("/dev/", "/proc/", "/sys/")
_SYSTEM_PREFIXES = ("/usr/", "/var/", "/etc/", "/opt/", "/bin/", "/sbin/",
                    "/lib/", "/private/etc/", "/System/", "/Library/")
_DIRECTIVE_RE = re.compile(
    r"\b(?:in|into|inside|under|within|at|fix|edit|update|modify|change|patch|"
    r"refactor|create|build|write|implement|repair|debug|work\s+(?:in|on))"
    r"\s+(?:the\s+(?:folder|directory|repo|project|code|file)\s+(?:in|at)\s+)?"
    r"[`'\"(]?$", re.I)


def _instruction_text(text: str) -> str:
    """The user's own instruction lines: fenced blocks and pasted log /
    traceback / prompt lines blanked out."""
    t = _FENCE_RE.sub(" ", str(text or ""))
    return "\n".join("" if _PASTED_LINE_RE.match(ln) else ln
                     for ln in t.splitlines())


def _raw_paths(text: str) -> list[str]:
    from .scope_guard import _USER_PATH_RE
    t = _instruction_text(text)
    out = []
    for m in _USER_PATH_RE.finditer(t):
        p = m.group(1).rstrip(".:!?*_`'\")]")
        if not p or p in out:
            continue
        probe = p if p.endswith("/") else p + "/"
        if probe.startswith(_NEVER_PREFIXES) or p in ("/dev", "/proc", "/sys"):
            continue
        if probe.startswith(_SYSTEM_PREFIXES):
            line_start = t.rfind("\n", 0, m.start()) + 1
            if not _DIRECTIVE_RE.search(t[line_start:m.start()]):
                continue                  # mentioned, not asked for
        out.append(p)
    return out


_ROOT_CACHE: dict[str, str] = {}
_ROOT_CACHE_MAX = 256


def _git_root(path: str) -> str:
    """The repo root above ``path`` — found on the filesystem (a ``.git``
    dir or file), no subprocess per path. A "repo" at ``~`` or above (a
    dotfiles repo) is not a project root.

    Only a FOUND root is cached (and re-checked: its ``.git`` must still be
    there). "Not a repo" is never cached — a folder the user let the team
    ``git init`` on turn 1 is a repo on turn 2, and a stale negative made it
    look new again: a second init, the user's current edits committed as a
    "baseline" and a fast-forward of a branch that was never fresh."""
    hit = _ROOT_CACHE.get(path)
    if hit and os.path.exists(os.path.join(hit, ".git")):
        return hit
    root = _find_git_root(path)
    if root:
        if len(_ROOT_CACHE) >= _ROOT_CACHE_MAX:
            _ROOT_CACHE.clear()
        _ROOT_CACHE[path] = root
    else:
        _ROOT_CACHE.pop(path, None)
    return root


_git_root.cache_clear = _ROOT_CACHE.clear  # type: ignore[attr-defined]


def _find_git_root(path: str) -> str:
    cur = path
    while cur and cur != os.sep:
        if _broad(cur):
            return ""
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


_BROAD_HOME = ("Documents", "Desktop", "Downloads", "Library", "Pictures",
               "Movies", "Music", "Public", "Applications", "Dropbox",
               "iCloud Drive", "OneDrive", "Google Drive")
_BROAD_SYSTEM = ("/tmp", "/var", "/private", "/private/tmp", "/private/var",
                 "/usr", "/usr/local", "/opt", "/etc", "/private/etc", "/srv",
                 "/mnt", "/media", "/Volumes", "/Users", "/home", "/root",
                 "/System", "/Library", "/Applications", "/bin", "/sbin",
                 "/lib", "/var/tmp", "/var/folders")


def _broad(path: str) -> bool:
    """``/``, the home directory or above it, a system directory, or a
    top-level personal folder (``~/Documents``) — never a build target: the
    team would commit everything in it."""
    home = os.path.realpath(os.path.expanduser("~"))
    p = path.rstrip(os.sep) or os.sep
    if p in (os.sep, home) or home.startswith(p + os.sep):
        return True
    if p in _BROAD_SYSTEM or p in {os.path.realpath(x) for x in _BROAD_SYSTEM
                                   if os.path.exists(x)}:
        return True
    return os.path.dirname(p) == home and os.path.basename(p) in _BROAD_HOME


def _looks_like_file(path: str) -> bool:
    base = os.path.basename(path.rstrip("/"))
    stem, ext = os.path.splitext(base)
    return bool(stem) and 1 < len(ext) <= _FILE_EXT_MAX


def _filesystem_shaped(real: str, raw: str) -> bool:
    """A missing path worth asking about: ``~/…``, or one whose nearest
    existing ancestor is a real folder below ``/`` (``/Users/me/gone``).
    ``/api/users`` (nearest existing ancestor is ``/``) is a URL route in a
    prompt, not a folder — ignored."""
    if raw.startswith("~"):
        return True
    cur = os.path.dirname(real)
    while cur and cur != os.sep:
        if os.path.isdir(cur):
            return True
        cur = os.path.dirname(cur)
    return False


def _inside(path: str, root: str) -> bool:
    root = root.rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def _root_for(raw: str) -> tuple[str, str, bool]:
    """``(named_dir, work_root, missing)`` for one path the user typed. Empty
    strings when the path is not a folder reference at all. ``work_root`` is
    the git root; empty when the folder is in no repo (the caller asks before
    initialising one)."""
    try:
        real = os.path.realpath(os.path.expanduser(raw))
    except Exception:  # noqa: BLE001
        return "", "", False
    if os.path.isdir(real):
        named = real
    elif os.path.isfile(real):
        named = os.path.dirname(real)
    elif _looks_like_file(real) and os.path.isdir(os.path.dirname(real)):
        named = os.path.dirname(real)          # a file to create in a real dir
    else:
        return "", "", _filesystem_shaped(real, raw)
    if named in ("", os.sep):
        return "", "", False
    git = _git_root(named)
    if not git and os.path.isfile(real):
        # A lone file outside any repo (/tmp/app.log): a thing to read, not a
        # project to build in.
        return "", "", False
    return named, git, False


def user_texts(prompt, history) -> list[str]:
    """The user's own words, newest first: this prompt, then earlier user
    turns (a follow-up keeps the folder its first turn named). The enhancer's
    restatement block is cut off — it is model output, not consent."""
    out = [str(prompt or "")]
    for m in reversed(list(history or [])):
        if not isinstance(m, dict) or (m.get("role") or "user") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(p.get("text") or "") for p in c
                         if isinstance(p, dict))
        t = str(c or "").split("\n\n---\n[Interpreted request")[0]
        if t and t not in out:
            out.append(t)
    return out


def resolve_team_target(texts, cwd: str) -> TeamTarget:
    """The directory a team/build run should work in. See module docstring.

    Only the NEWEST text that names any folder decides (an older turn's folder
    is kept only while the new message names none). Paths inside ``cwd`` leave
    the target unchanged. Fail-open: any error → the input ``cwd``."""
    base = os.path.realpath(cwd) if cwd else ""
    res = TeamTarget(cwd=cwd)
    try:
        for text in texts or ():
            roots: list[tuple[str, str]] = []
            missing: list[str] = []
            ignored: list[str] = []
            for raw in _raw_paths(text):
                named, root, miss = _root_for(raw)
                if miss:
                    missing.append(raw)
                elif named and _broad(root or named):
                    ignored.append(raw)
                elif named and (root, named) not in roots:
                    roots.append((root, named))
            if not roots and not missing:
                res.ignored = ignored
                if ignored:
                    return res
                continue                       # this turn named no folder
            outside = [(r, n) for r, n in roots
                       if not (base and _inside(n, base))]
            res.ignored = ignored
            if missing and not outside:
                res.missing = missing
                return res
            if outside:
                root, res.named = outside[0]
                res.cwd = root or res.named
                res.init_needed = not root
                res.others = sorted({r or n for r, n in outside[1:]} - {res.cwd})
                res.missing = missing
            return res
    except Exception as exc:  # noqa: BLE001 — never break a turn over this
        log.debug("team target resolution skipped: %s", exc)
    return res


def init_question(folder: str) -> str:
    """Asked before a team run initialises git in a folder the user named."""
    return (f"`{folder}` is not a git repository. The team commits every step "
            "with git, so running there means `git init` plus a baseline commit "
            "of what is in it now. Allow that?")


def clarify_text(missing) -> str:
    """The question asked instead of building in an invented folder."""
    listed = ", ".join(f"`{m}`" for m in missing[:4])
    return (f"I can't find {listed} on this machine, so I haven't started the "
            "build — I won't create a new folder with that name in the session "
            "workspace and report it as your project. Please check the path "
            "(it must exist where AIForge runs), or tell me to create it.")


def anchor_subtask_paths(subs: list, cwd: str, aliases=None) -> tuple[list, list]:
    """Rewrite each subtask ``path`` so it is relative to ``cwd``.

    An absolute path under ``cwd`` (or one the planner already stripped of its
    leading ``/`` — ``Users/me/proj/money.py`` when cwd is ``/Users/me/proj``)
    becomes relative. ``aliases`` are folders that stand for ``cwd``: a team
    run in the user's repo works in its own worktree, so the planner's
    ``/Users/me/proj/money.py`` is ``money.py`` of the worktree (default: the
    repo of the run registered for ``cwd``). A path that points OUTSIDE all of
    them is dropped: writing it as a relative path would create a literal
    ``Users/me/...`` folder in the workspace. Returns ``(subs, dropped_paths)``."""
    if aliases is None:
        aliases = _run_repo_aliases(cwd)
    bases = [os.path.realpath(cwd).rstrip(os.sep)]
    bases += [os.path.realpath(a).rstrip(os.sep) for a in aliases or () if a]
    kept, dropped = [], []
    for s in subs or []:
        p = str((s or {}).get("path") or "").strip()
        if not p:
            kept.append(s)
            continue
        cand = os.path.expanduser(p)
        rel = _rel_to_bases(cand, bases)
        if rel is not None:
            s = dict(s, path=rel)
        elif os.path.isabs(cand) or _stripped_home_path(cand, bases):
            dropped.append(p)
            continue
        kept.append(s)
    return kept, dropped


def _rel_to_bases(cand: str, bases: list) -> str | None:
    """``cand`` relative to the first base it lies in — given absolute, or
    with the base's leading ``/`` stripped. None otherwise."""
    if os.path.isabs(cand):
        real = os.path.realpath(cand)
        for b in bases:
            for c in (real, cand.rstrip(os.sep)):
                if _inside(c, b):
                    return os.path.relpath(c, b)
        return None
    for b in bases:
        rb = b.lstrip(os.sep)
        if rb and (cand == rb or cand.startswith(rb + "/")):
            return cand[len(rb):].lstrip("/") or "."
    return None


def _run_repo_aliases(cwd: str) -> list:
    try:
        from .team_workspace import for_cwd
        ws = for_cwd(cwd)
    except Exception:  # noqa: BLE001
        ws = None
    return [ws.repo] if ws is not None and ws.repo else []


def localize_paths(text: str, repo: str) -> str:
    """The user's words with every path under ``repo`` rewritten relative to
    it (``/Users/me/proj/money.py`` → ``money.py``; the folder itself → ``./``).

    A team run works in its own worktree: an absolute repo path left in the
    prompt or SPEC sends a writer into the user's real checkout."""
    from .scope_guard import _USER_PATH_RE
    from .team_run_life import fold
    if not text or not repo:
        return text
    frepo = fold(repo)

    def _sub(m):
        raw = m.group(1)
        p = raw.rstrip(".:!?*_`'\")]")
        rest = raw[len(p):]
        try:
            real = os.path.realpath(os.path.expanduser(p)).rstrip(os.sep)
        except Exception:  # noqa: BLE001
            return raw
        freal = fold(real)
        if freal != frepo and not freal.startswith(frepo + os.sep):
            return raw        # (case-insensitively on macOS — see fold)
        rel = real[len(frepo):].lstrip(os.sep)
        if not rel:
            return "./" + rest
        return rel + ("/" if p.endswith("/") else "") + rest
    return _USER_PATH_RE.sub(_sub, str(text))


def _stripped_home_path(rel: str, bases) -> bool:
    """``Users/me/other/x.py`` — the user's home folder with its leading ``/``
    stripped, pointing outside every folder in ``bases``. Only that shape is
    dropped: a real relative path such as ``var/lib/x.py`` or ``etc/config.py`` is a file the
    project may well contain."""
    home = os.path.realpath(os.path.expanduser("~")).strip(os.sep)
    if not home or not (rel == home or rel.startswith(home + "/")):
        return False
    if isinstance(bases, str):
        bases = [bases]
    real = os.path.realpath(os.sep + rel)
    return not any(_inside(real, b) for b in bases)


__all__ = ["TeamTarget", "anchor_subtask_paths", "clarify_text",
           "init_question", "localize_paths", "resolve_team_target",
           "user_texts"]
