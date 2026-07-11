"""OKR node store — the typed folder layout under ``<memory>/okr/``, SEGREGATED
by scope into a GLOBAL subtree and one subtree per PROJECT (repo/workspace):

    okr/
      global/
        objectives/  key_results/  learnings/  sessions/  solutions/
      projects/<workspace>/
        objectives/  key_results/  learnings/  sessions/  solutions/
      index.md   (## Global / ## <workspace> …)  log.md

A node's scope is DERIVED from its frontmatter (a solution's ``workspace``, a
learning's ``workspace``/``scope: repo:<name>``, else global). Ids stay globally
unique per type (S-01, L-03) so cross-scope edges keep resolving; the folder is
organization only. Reads walk global + every project (and legacy flat
``okr/<dir>/`` until :func:`migrate_scoped` moves them), so nothing breaks
mid-migration. All reads soft-fail (a missing/half-written file is skipped).
"""
from __future__ import annotations

import os
import re

from . import nodes as _n

# type → folder name.
_DIR = {"objective": "objectives", "key_result": "key_results",
        "learning": "learnings", "session": "sessions",
        "solution": "solutions"}


def okr_root() -> str:
    from aiforge_core.memory.md_store import memory_dir
    return os.path.join(str(memory_dir()), "okr")


def _scope_slug(s) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "").strip()).strip("-")


def _scope_of(node_type: str, meta: dict | None) -> str:
    """Derive a node's scope slug from its frontmatter: a project name, or ""
    for global. Solutions/sessions/objectives use ``workspace`` (or ``repo``);
    learnings additionally honour ``scope: repo:<name>``. Everything else is
    global (a cross-project goal or rule)."""
    m = meta or {}
    ws = m.get("workspace") or m.get("repo") or ""
    if ws:
        return _scope_slug(ws)
    sc = m.get("scope")
    if isinstance(sc, str) and sc.lower().startswith("repo:"):
        return _scope_slug(sc.split(":", 1)[1])
    return ""


def _scope_bases() -> list[str]:
    """All scope roots that currently exist: global/ + each projects/<name>/."""
    root = okr_root()
    bases = [os.path.join(root, "global")]
    pdir = os.path.join(root, "projects")
    if os.path.isdir(pdir):
        bases += [os.path.join(pdir, n) for n in sorted(os.listdir(pdir))
                  if os.path.isdir(os.path.join(pdir, n))]
    return bases


def okr_scopes() -> list[str]:
    """Project workspace names that have an OKR subtree (excludes global)."""
    pdir = os.path.join(okr_root(), "projects")
    if not os.path.isdir(pdir):
        return []
    return sorted(n for n in os.listdir(pdir)
                  if os.path.isdir(os.path.join(pdir, n)))


def _scope_label_from_path(path: str) -> str:
    """'Global' or the project name, inferred from a node file's path."""
    parts = os.path.normpath(path).split(os.sep)
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "Global"


def type_dir(node_type: str, scope: str = "") -> str:
    """Folder for ``node_type`` in ``scope`` (""/None → global). Created lazily."""
    scope = _scope_slug(scope)
    sub = os.path.join("projects", scope) if scope else "global"
    d = os.path.join(okr_root(), sub, _DIR.get(node_type, "misc"))
    os.makedirs(d, exist_ok=True)
    return d


def _filename(node_type: str, node_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(node_id)).strip("-") or "node"
    return f"{safe}.md"


def _find_node_files(node_type: str, node_id: str) -> list[str]:
    """Every on-disk file for this id (across scopes + legacy flat) — used to
    de-dupe when a node's scope changes (e.g. a workspace is added later)."""
    fn = _filename(node_type, node_id)
    return [f for f in _list_files(node_type) if os.path.basename(f) == fn]


def next_id(node_type: str) -> str:
    """Allocate the next id for a type. objective→O-01, key_result→KR-01,
    learning→L-01; session→<UTC-date>-NN (unique within the day)."""
    if node_type == "session":
        import datetime as _dt
        day = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        n = 1 + sum(1 for f in _list_files("session")
                    if os.path.basename(f).startswith(day))
        return f"{day}-{n:02d}"
    prefix = _n._ID_PREFIX.get(node_type, "N")
    top = 0
    rx = re.compile(rf"^{prefix}-(\d+)$")
    for f in _list_files(node_type):
        m = rx.match(os.path.splitext(os.path.basename(f))[0])
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}-{top + 1:02d}"


def _list_files(node_type: str) -> list[str]:
    """Every file for ``node_type`` across the global + all project subtrees,
    PLUS the legacy flat ``okr/<dir>/`` (read until migrate_scoped moves it)."""
    dirname = _DIR.get(node_type, "misc")
    out: list[str] = []
    for base in _scope_bases():
        d = os.path.join(base, dirname)
        if os.path.isdir(d):
            out += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
    legacy = os.path.join(okr_root(), dirname)   # pre-segregation location
    if os.path.isdir(legacy):
        out += [os.path.join(legacy, f) for f in os.listdir(legacy)
                if f.endswith(".md")]
    return out


def save_node(node_type: str, node_id: str | None, meta: dict,
              body: str = "") -> dict:
    """Render + write a node atomically. ``node_id=None`` allocates one.
    Returns ``{ok, id, path}`` (soft-fail)."""
    if node_type not in _n.NODE_TYPES:
        return {"ok": False, "error": f"unknown type {node_type!r}"}
    import contextlib
    nid = str(node_id or next_id(node_type))
    scope = _scope_of(node_type, meta)               # global "" or a project
    path = os.path.join(type_dir(node_type, scope), _filename(node_type, nid))
    # De-dupe across scopes: if this id already lives elsewhere (a legacy flat
    # file, or a different scope because a workspace was just added), drop the
    # stale copy so the node exists in exactly ONE place.
    for old in _find_node_files(node_type, nid):
        if os.path.abspath(old) != os.path.abspath(path):
            with contextlib.suppress(OSError):
                os.unlink(old)
    text = _n.render_node(node_type, nid, meta, body)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return {"ok": False, "error": f"write failed: {exc}"}
    _write_index()   # keep the reserved OKF navigation file fresh
    return {"ok": True, "id": nid, "path": path, "scope": scope or "global"}


def _write_index() -> str:
    """(Re)generate the reserved OKF ``index.md`` at the bundle root — pure
    navigation, NO frontmatter (spec §3), GROUPED by scope: a ``## Global``
    section then one ``## <workspace>`` section per project, each listing its
    concepts with absolute bundle-relative links. Soft-fail (best-effort)."""
    try:
        root = okr_root()
        by_scope: dict[str, list[tuple[str, str]]] = {}
        for d in load_all():
            p = d.get("path", "")
            rel = "/" + os.path.relpath(p, root).replace(os.sep, "/")
            meta = d.get("meta") or {}
            hook = (meta.get("title") or meta.get("description")
                    or (d.get("body") or "").strip().split("\n", 1)[0])[:80]
            by_scope.setdefault(_scope_label_from_path(p), []).append((rel, hook))
        order = ["Global"] + sorted(k for k in by_scope if k != "Global")
        lines = ["# OKR Knowledge Bundle", ""]
        for label in order:
            items = by_scope.get(label)
            if not items:
                continue
            lines.append(f"## {label}")
            for rel, hook in sorted(items):
                lines.append(f"- [{rel.rsplit('/', 1)[-1]}]({rel})"
                             + (f" — {hook}" if hook else ""))
            lines.append("")
        idx = os.path.join(root, "index.md")
        with open(idx, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
        return idx
    except Exception:  # noqa: BLE001 — index is navigation, never block a save
        return ""


def read_node(node_type: str, node_id: str) -> dict | None:
    fn = _filename(node_type, node_id)
    for path in _find_node_files(node_type, node_id):
        if os.path.basename(path) != fn:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                d = _n.parse_node(fh.read())
            d["path"] = path
            return d
        except OSError:
            continue
    return None


def load_all(scope: str | None = None) -> list[dict]:
    """Every OKR node across all scopes (parsed). ``scope`` filters: None = all,
    "global" = the global subtree only, "<workspace>" = that project only.
    Each dict is ``{type, id, meta, body, path}``. Skips unreadable files."""
    want = None if scope is None else (_scope_slug(scope) or "global")
    out: list[dict] = []
    for t in _n.NODE_TYPES:
        for f in _list_files(t):
            if want is not None:
                lbl = _scope_label_from_path(f)
                lbl = "global" if lbl == "Global" else _scope_slug(lbl)
                if lbl != want:
                    continue
            try:
                with open(f, encoding="utf-8") as fh:
                    d = _n.parse_node(fh.read())
                if not d.get("type"):
                    d["type"] = t
                if not d.get("id"):
                    d["id"] = os.path.splitext(os.path.basename(f))[0]
                d["path"] = f
                out.append(d)
            except OSError:
                continue
    return out


def migrate_scoped() -> dict:
    """One-shot: move legacy FLAT ``okr/<dir>/*.md`` nodes into their scoped home
    (global/ or projects/<workspace>/) by each node's derived scope. Idempotent
    (already-scoped nodes are untouched); empty legacy dirs are removed; the
    index is rebuilt. Soft-fail. Returns ``{ok, moved, scopes}``."""
    import shutil
    root = okr_root()
    moved = 0
    for t in _n.NODE_TYPES:
        legacy = os.path.join(root, _DIR.get(t, "misc"))
        if not os.path.isdir(legacy):
            continue
        for f in list(os.listdir(legacy)):
            if not f.endswith(".md"):
                continue
            src = os.path.join(legacy, f)
            try:
                with open(src, encoding="utf-8") as fh:
                    d = _n.parse_node(fh.read())
            except OSError:
                continue
            dst = os.path.join(type_dir(t, _scope_of(t, d.get("meta") or {})), f)
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            try:
                shutil.move(src, dst)
                moved += 1
            except OSError:
                continue
        try:
            if not os.listdir(legacy):
                os.rmdir(legacy)
        except OSError:
            pass
    _write_index()
    return {"ok": True, "moved": moved, "scopes": okr_scopes()}


__all__ = ["okr_root", "type_dir", "next_id", "save_node", "read_node",
           "load_all", "okr_scopes", "migrate_scoped"]
