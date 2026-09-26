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
import subprocess
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

    @property
    def retargeted(self) -> bool:
        return bool(self.named)


def _raw_paths(text: str) -> list[str]:
    from .scope_guard import _USER_PATH_RE
    out = []
    for raw in _USER_PATH_RE.findall(str(text or "")):
        p = raw.rstrip(".:!?*_`'\")]")
        if p and p not in out:
            out.append(p)
    return out


def _git_root(path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except Exception:  # noqa: BLE001 — git missing / timeout
        pass
    return ""


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
    strings when the path is not a folder reference at all."""
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
    return named, (git or named), False


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
            for raw in _raw_paths(text):
                named, root, miss = _root_for(raw)
                if miss:
                    missing.append(raw)
                elif root and (root, named) not in roots:
                    roots.append((root, named))
            if not roots and not missing:
                continue                       # this turn named no folder
            outside = [(r, n) for r, n in roots
                       if not (base and _inside(n, base))]
            if missing and not outside:
                res.missing = missing
                return res
            if outside:
                res.cwd, res.named = outside[0]
                res.others = sorted({r for r, _ in outside[1:]} - {res.cwd})
                res.missing = missing
            return res
    except Exception as exc:  # noqa: BLE001 — never break a turn over this
        log.debug("team target resolution skipped: %s", exc)
    return res


def clarify_text(missing) -> str:
    """The question asked instead of building in an invented folder."""
    listed = ", ".join(f"`{m}`" for m in missing[:4])
    return (f"I can't find {listed} on this machine, so I haven't started the "
            "build — I won't create a new folder with that name in the session "
            "workspace and report it as your project. Please check the path "
            "(it must exist where AIForge runs), or tell me to create it.")


def anchor_subtask_paths(subs: list, cwd: str) -> tuple[list, list]:
    """Rewrite each subtask ``path`` so it is relative to ``cwd``.

    An absolute path under ``cwd`` (or one the planner already stripped of its
    leading ``/`` — ``Users/me/proj/money.py`` when cwd is ``/Users/me/proj``)
    becomes relative. A path that points OUTSIDE ``cwd`` is dropped: writing
    it as a relative path would create a literal ``Users/me/...`` folder in
    the workspace. Returns ``(subs, dropped_paths)``."""
    base = os.path.realpath(cwd).rstrip(os.sep)
    rel_base = base.lstrip(os.sep)
    kept, dropped = [], []
    for s in subs or []:
        p = str((s or {}).get("path") or "").strip()
        if not p:
            kept.append(s)
            continue
        cand = os.path.expanduser(p)
        if os.path.isabs(cand):
            real = os.path.realpath(cand)
            if _inside(real, base):
                s = dict(s, path=os.path.relpath(real, base))
            else:
                dropped.append(p)
                continue
        elif rel_base and (cand == rel_base or cand.startswith(rel_base + "/")):
            s = dict(s, path=cand[len(rel_base):].lstrip("/") or ".")
        elif _outside_shaped(cand, base):
            dropped.append(p)
            continue
        kept.append(s)
    return kept, dropped


def _outside_shaped(rel: str, base: str) -> bool:
    """``Users/me/other/x.py`` — a stripped absolute path to a folder that
    exists outside ``base`` (its first two segments exist at ``/``)."""
    parts = [x for x in rel.split("/") if x]
    if len(parts) < 3:
        return False
    head = os.sep + os.path.join(parts[0], parts[1])
    return os.path.isdir(head) and not _inside(os.path.realpath(head), base)


__all__ = ["TeamTarget", "anchor_subtask_paths", "clarify_text",
           "resolve_team_target", "user_texts"]
