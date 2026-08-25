"""Persistent repo → local-folder mapping.

Answers two operator needs that were previously un-storable:

  1. **Global base folder** for all repos — ``default_root`` (falls back to the
     ``AIFORGE_WORKTREE_ROOT`` env, then ``~/codeRepo``). A ticket whose
     ``project`` is ``foo`` resolves to ``<default_root>/foo``.
  2. **Per-repo explicit path** — ``paths[name] = /abs/path`` for a repo that
     lives OUTSIDE the base folder. This wins over the base-folder guess.

Stored as JSON at ``$AIFORGE_CONFIG_DIR/repos.json`` (same config dir as
integrations). Set from chat ("use /x/y for repo foo", "the base folder for all
repos is /x") via the ``set_repo_folder`` / ``set_repo_root`` tools, or the API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from aiforge_core.config.paths import config_dir


def _path() -> Path:
    cfg = str(config_dir())
    return Path(cfg) / "repos.json"


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return {}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")


def default_root() -> str:
    """Global base folder for all repos. Stored value wins, else the
    ``AIFORGE_WORKTREE_ROOT`` env (read live, not the frozen constant), else
    ``~/codeRepo``."""
    stored = (_load().get("default_root") or "").strip()
    if stored:
        return os.path.expanduser(stored)
    return os.path.expanduser(os.environ.get("AIFORGE_WORKTREE_ROOT", "~/codeRepo"))


def set_default_root(path: str) -> dict:
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "path required"}
    d = _load()
    d["default_root"] = os.path.expanduser(path)
    _save(d)
    return {"ok": True, "default_root": d["default_root"]}


def get_path(name: str) -> str | None:
    """Explicit folder for repo ``name`` (case-insensitive), or None."""
    if not name:
        return None
    paths = _load().get("paths") or {}
    if name in paths:
        return os.path.expanduser(paths[name])
    low = name.strip().lower()
    for k, v in paths.items():
        if k.lower() == low:
            return os.path.expanduser(v)
    return None


def set_path(name: str, path: str) -> dict:
    name = (name or "").strip()
    path = (path or "").strip()
    if not name or not path:
        return {"ok": False, "error": "name and path required"}
    d = _load()
    d.setdefault("paths", {})[name] = os.path.expanduser(path)
    _save(d)
    return {"ok": True, "name": name, "path": d["paths"][name]}


def delete_path(name: str) -> dict:
    d = _load()
    paths = d.get("paths") or {}
    if name in paths:
        del paths[name]
        _save(d)
        return {"ok": True, "removed": name}
    return {"ok": False, "error": f"no mapping for {name!r}"}


def _norm(s: str) -> str:
    """Loose key: lowercase, drop every non-alphanumeric — so 'Pos Client-Backend',
    'posclientbackend', and 'pos_client backend' all collapse to the same token."""
    import re
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _candidates() -> dict:
    """name → abs path for every known repo: explicit mappings + the folders
    directly under default_root."""
    cands: dict[str, str] = {}
    for k, v in (_load().get("paths") or {}).items():
        cands[k] = os.path.expanduser(v)
    root = default_root()
    try:
        if os.path.isdir(root):
            for e in os.scandir(root):
                if e.is_dir() and not e.name.startswith("."):
                    cands.setdefault(e.name, e.path)
    except OSError:
        pass
    return cands


def _fuzzy_exact(want: str, cands: dict, one, distinct) -> "dict | None":
    """Normalized-exact match stage: a single (or all-aliased) exact hit wins;
    several distinct exact hits are ambiguous. None → fall through to fuzzy."""
    hit = [(n, v) for n, v in cands.items() if _norm(n) == want]
    if hit and distinct(hit):
        return {**one(hit[0][0], hit[0][1]), "match": "normalized"}
    if len(hit) > 1:
        return {"ok": False, "error": "ambiguous", "candidates": [n for n, _ in hit]}
    return None


def _fuzzy_close(want: str, cands: dict, cutoff: float, one, distinct) -> "dict | None":
    """difflib fuzzy stage. A clear winner (only match, or a >=0.08 ratio gap, or
    all-aliased) is picked; otherwise ambiguous. None → fall through to substring."""
    import difflib
    normmap: dict = {}
    for n, v in cands.items():
        normmap.setdefault(_norm(n), (n, v))
    close = difflib.get_close_matches(want, list(normmap), n=3, cutoff=cutoff)
    if not close:
        return None
    top = difflib.SequenceMatcher(None, want, close[0]).ratio()
    second = (difflib.SequenceMatcher(None, want, close[1]).ratio()
              if len(close) > 1 else 0.0)
    picks = [normmap[c] for c in close]
    if len(close) == 1 or top - second >= 0.08 or distinct(picks):
        n, v = normmap[close[0]]
        return {**one(n, v), "match": "fuzzy", "candidates": [p[0] for p in picks]}
    return {"ok": False, "error": "ambiguous", "candidates": [p[0] for p in picks]}


def _fuzzy_substring(want: str, cands: dict, one, distinct) -> dict:
    """Substring stage (last resort): a single distinct containment hit wins."""
    subs = [(n, v) for n, v in cands.items()
            if want and (want in _norm(n) or _norm(n) in want)]
    if subs and distinct(subs):
        return {**one(subs[0][0], subs[0][1]), "match": "substring"}
    if len(subs) > 1:
        return {"ok": False, "error": "ambiguous", "candidates": [n for n, _ in subs]}
    return {"ok": False, "error": "no match", "candidates": sorted(cands)[:10]}


def fuzzy_pick(name: str, cands: dict, *, cutoff: float = 0.7,
               value_key: str = "value") -> dict:
    """Generic loose-name matcher shared by repo folders, Jira projects, and
    Confluence spaces. ``cands`` = {display_name: value}. Tolerates case, spaces,
    missing hyphens/underscores, and small typos. Order: normalized-exact →
    fuzzy (difflib) → substring. Returns ``{ok, <value_key>, name, match}`` on a
    confident pick, else ``{ok:False, error, candidates}`` (ambiguous/none) so
    the caller can ask. Aliases (several names → same value) collapse to one hit."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required", "candidates": []}
    if not cands:
        return {"ok": False, "error": "no candidates", "candidates": []}

    def _one(disp, val):
        return {"ok": True, value_key: val, "name": disp}

    def _distinct(pairs):
        return len({v for _, v in pairs}) == 1   # all point at the SAME value

    want = _norm(name)
    return (_fuzzy_exact(want, cands, _one, _distinct)
            or _fuzzy_close(want, cands, cutoff, _one, _distinct)
            or _fuzzy_substring(want, cands, _one, _distinct))


def resolve(name: str, *, cutoff: float = 0.7) -> dict:
    """Find a repo's folder from a LOOSELY-typed name (case, spaces, missing
    hyphens, small typos). Explicit mapping wins; else fuzzy over the explicit
    mappings + the folders under default_root. Returns
    ``{ok, path, name, match, candidates}``."""
    name = (name or "").strip()
    exact = get_path(name)
    if exact and os.path.isdir(exact):
        return {"ok": True, "path": exact, "name": name, "match": "explicit"}
    cands = _candidates()
    if not cands:
        return {"ok": False, "error": "no repos found "
                "(set the base folder with set_repo_root)", "candidates": []}
    return fuzzy_pick(name, cands, cutoff=cutoff, value_key="path")


def list_all() -> dict:
    d = _load()
    return {"default_root": default_root(), "paths": d.get("paths") or {}}
