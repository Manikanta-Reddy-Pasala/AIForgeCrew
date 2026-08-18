"""OKF node store — the typed folder layout under ``<memory>/okf/``, SEGREGATED
by scope into a GLOBAL subtree and one subtree per PROJECT (repo/workspace):

    okf/
      global/
        objectives/  key_results/  learnings/  sessions/  solutions/
      projects/<workspace>/
        objectives/  key_results/  learnings/  sessions/  solutions/
      index.md   (## Global / ## <workspace> …)  log.md

A node's scope is DERIVED from its frontmatter (a solution's ``workspace``, a
learning's ``workspace``/``scope: repo:<name>``, else global). Ids stay globally
unique per type (S-01, L-03) so cross-scope edges keep resolving; the folder is
organization only. Reads walk global + every project (and legacy flat
``okf/<dir>/`` until :func:`migrate_scoped` moves them), so nothing breaks
mid-migration. All reads soft-fail (a missing/half-written file is skipped).
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from pathlib import Path

from . import nodes as _n

_log = logging.getLogger("aiforge.memory.okf")

# Serialize node mutations (write + de-dupe + index) so two concurrent chat
# turns authoring solutions can't interleave a dedup-miss into a double write or
# clobber the index. Re-entrant: save_node → _write_index → load_all all under it.
_LOCK = threading.RLock()

# Parse cache: load_all re-parses EVERY node file on every call (graph.build,
# retrieve×2/turn, dedup, the index rewrite). Cache the full parsed set keyed on
# a cheap directory signature (newest mtime + file count → catches add/edit/
# delete); scope filtering runs in-memory over the cached list.
_CACHE: dict = {"sig": None, "all": None}


def _invalidate() -> None:
    """Drop the parse cache — called after any node file mutation so the next
    read reparses (deterministic; not reliant on mtime granularity)."""
    with _LOCK:
        _CACHE["sig"] = None

# type → folder name.
_DIR = {"objective": "objectives", "key_result": "key_results",
        "learning": "learnings", "session": "sessions",
        "solution": "solutions", "repo": "repo", "script": "scripts",
        "task": "tasks"}


def okf_root() -> str:
    from aiforge_core.memory.md_store import memory_dir
    return os.path.join(str(memory_dir()), "okf")


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
    root = okf_root()
    bases = [os.path.join(root, "global")]
    pdir = os.path.join(root, "projects")
    if os.path.isdir(pdir):
        bases += [os.path.join(pdir, n) for n in sorted(os.listdir(pdir))
                  if os.path.isdir(os.path.join(pdir, n))]
    return bases


def okr_scopes() -> list[str]:
    """Project workspace names that have an OKR subtree (excludes global)."""
    pdir = os.path.join(okf_root(), "projects")
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
    d = os.path.join(okf_root(), sub, _DIR.get(node_type, "misc"))
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
    PLUS the legacy flat ``okf/<dir>/`` (read until migrate_scoped moves it)."""
    dirname = _DIR.get(node_type, "misc")
    out: list[str] = []
    for base in _scope_bases():
        d = os.path.join(base, dirname)
        if os.path.isdir(d):
            out += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
    legacy = os.path.join(okf_root(), dirname)   # pre-segregation location
    if os.path.isdir(legacy):
        out += [os.path.join(legacy, f) for f in os.listdir(legacy)
                if f.endswith(".md")]
    return out


def save_node(node_type: str, node_id: str | None, meta: dict,
              body: str = "", *, reindex: bool = True) -> dict:
    """Render + write a node atomically. ``node_id=None`` allocates one.
    ``reindex=False`` skips the (O(N)) index rewrite — bulk callers pass it and
    call :func:`_write_index` ONCE at the end instead of N times. Returns
    ``{ok, id, path, scope}`` (soft-fail)."""
    if node_type not in _n.NODE_TYPES:
        return {"ok": False, "error": f"unknown type {node_type!r}"}
    import contextlib
    with _LOCK:
        nid = str(node_id or next_id(node_type))
        scope = _scope_of(node_type, meta)           # global "" or a project
        path = os.path.join(type_dir(node_type, scope), _filename(node_type, nid))
        # De-dupe across scopes: if this id already lives elsewhere (a legacy
        # flat file, or a different scope because a workspace was just added),
        # drop the stale copy so the node exists in exactly ONE place.
        for old in _find_node_files(node_type, nid):
            if os.path.abspath(old) != os.path.abspath(path):
                with contextlib.suppress(OSError):
                    os.unlink(old)
        from aiforge_core.memory.sync import _io as _sync_io
        from aiforge_core.memory.sync.identity import stamp as _stamp

        meta = _stamp(meta or {})
        text = _n.render_node(node_type, nid, meta, body)
        try:
            # One atomic-write implementation for the whole memory tree: a
            # sync applier and this function write the same files, and a
            # per-caller ".tmp" name is exactly what lets them tear.
            _sync_io.write_atomic(Path(path), text.encode("utf-8"))
        except OSError as exc:
            return {"ok": False, "error": f"write failed: {exc}"}
        _invalidate()        # a node changed → next read reparses
        if reindex:
            _write_index()   # keep the reserved OKF navigation file fresh
    return {"ok": True, "id": nid, "path": path, "scope": scope or "global"}


def _write_index() -> str:
    """(Re)generate the reserved OKF ``index.md`` at the bundle root — pure
    navigation, NO frontmatter (spec §3), GROUPED by scope: a ``## Global``
    section then one ``## <workspace>`` section per project, each listing its
    concepts with absolute bundle-relative links. Soft-fail (best-effort)."""
    try:
        root = okf_root()
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
        from aiforge_core.memory.sync import _io as _sync_io

        idx = os.path.join(root, "index.md")
        # Atomic: a reader never sees a half-written index, and two savers
        # racing here stage into separate temp files.
        _sync_io.write_atomic(Path(idx), ("\n".join(lines).rstrip() + "\n").encode("utf-8"))
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


def _dir_signature() -> tuple:
    """A CHEAP fingerprint of the whole bundle: (newest mtime, .md count).
    Changes on any add / edit / delete, so it invalidates the parse cache
    without re-reading file contents (stat only)."""
    root = okf_root()
    newest = 0.0
    n = 0
    for dp, _dn, fns in (os.walk(root) if os.path.isdir(root) else []):
        for f in fns:
            if f.endswith(".md"):
                n += 1
                with contextlib.suppress(OSError):
                    newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
    # include the ROOT so two different memory dirs with the same (mtime, count)
    # never collide (test isolation / a re-pointed AIFORGE_MEMORY_MD_DIR).
    return (root, round(newest, 3), n)


def _load_all_uncached() -> list[dict]:
    out: list[dict] = []
    for t in _n.NODE_TYPES:
        for f in _list_files(t):
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


def load_all(scope: str | None = None) -> list[dict]:
    """Every OKR node across all scopes (parsed). ``scope`` filters: None = all,
    "global" = the global subtree only, "<workspace>" = that project only.
    Each dict is ``{type, id, meta, body, path}``. Skips unreadable files.

    The full parse is CACHED on a cheap dir signature — so repeated reads in a
    turn (retrieve does global+repo; the index rewrite; dedup) parse the files
    at most once until something changes. Scope filtering runs in-memory."""
    with _LOCK:
        sig = _dir_signature()
        if _CACHE["sig"] != sig or _CACHE["all"] is None:
            _CACHE["all"] = _load_all_uncached()
            _CACHE["sig"] = sig
        data = _CACHE["all"]
    if scope is None:
        return list(data)
    want = _scope_slug(scope) or "global"
    out = []
    for d in data:
        lbl = _scope_label_from_path(d.get("path", ""))
        lbl = "global" if lbl == "Global" else _scope_slug(lbl)
        if lbl == want:
            out.append(d)
    return out


def migrate_scoped() -> dict:
    """One-shot: move legacy FLAT ``okf/<dir>/*.md`` nodes into their scoped home
    (global/ or projects/<workspace>/) by each node's derived scope. Idempotent
    (already-scoped nodes are untouched); empty legacy dirs are removed; the
    index is rebuilt. Soft-fail. Returns ``{ok, moved, scopes}``."""
    import shutil
    root = okf_root()
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
    _invalidate()          # files moved on disk → drop stale parse cache
    _write_index()
    return {"ok": True, "moved": moved, "scopes": okr_scopes()}


def fold_session_scopes_to_global() -> dict:
    """Repair phantom ``projects/session-<id>/`` OKR scopes — an unpinned chat's
    isolated scratch workspace that was mis-scoped as a project (one bogus
    "project" per chat session). Relocates every node under a ``session-<id>``
    scope into GLOBAL (fresh id, drop the old file), then dedupes the merged
    globals and removes the now-empty ``session-*`` project dirs. Idempotent;
    soft-fail. Returns ``{ok, moved, removed, dirs}``."""
    import shutil
    moved = 0
    try:
        for d in list(load_all()):
            label = str(_scope_label_from_path(d.get("path", "")))
            if not re.match(r"^session-\d+$", label):
                continue
            meta = dict(d.get("meta") or {})
            meta.pop("workspace", None)
            meta["scope"] = "global"
            save_node(d.get("type"), None, meta, d.get("body") or "",
                      reindex=False)
            with contextlib.suppress(OSError):
                os.unlink(d["path"])
            moved += 1
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "moved": moved}
    ded = dedupe_nodes()          # collapse the merged paraphrases in global
    dirs = 0
    pdir = os.path.join(okf_root(), "projects")
    if os.path.isdir(pdir):
        for n in list(os.listdir(pdir)):
            if not re.match(r"^session-\d+$", n):
                continue
            sub = os.path.join(pdir, n)
            # only remove once it holds no .md files (dedupe emptied it)
            if not any(f.endswith(".md") for _r, _ds, fs in os.walk(sub)
                       for f in fs):
                with contextlib.suppress(OSError):
                    shutil.rmtree(sub)
                    dirs += 1
    if moved or dirs:
        _invalidate()
        _write_index()
    return {"ok": True, "moved": moved, "removed": ded.get("removed", 0),
            "dirs": dirs}


def _norm_concept(s: str) -> str:
    """Normalize concept text for identity comparison — lowercase, keep only
    alphanumerics + spaces, collapse whitespace. Shared by the write-time
    concept lookup and the post-hoc dedupe so both agree on 'same concept'."""
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9 ]", "", (s or "").lower())).strip()


def _concept_of(d: dict) -> str:
    """The concept text of a parsed node — body first (learnings/facts share the
    same rule text even when titles differ), else description, else title."""
    m = d.get("meta") or {}
    return _norm_concept(d.get("body") or m.get("description") or m.get("title") or "")


def find_by_concept(node_type: str, meta: dict, concept_text: str,
                    *, threshold: float | None = None) -> str | None:
    """Return the id of an EXISTING node of the same ``node_type`` and the same
    resolved SCOPE whose concept text matches ``concept_text`` — exactly, or
    fuzzily above ``threshold``. Lets a writer REUSE the concept's file (pass the
    id back to :func:`save_node`) instead of minting a new incrementing id, so
    types with no natural title key (learnings, key_results) still honour OKF
    'one concept = one file' (≤1 per scope → ≤2 total: global + project).
    ``None`` when no match. Soft-fail (returns None on any error)."""
    if threshold is None:
        try:
            threshold = float(os.environ.get("AIFORGE_OKF_CONCEPT_SIMILARITY", "0.86"))
        except (TypeError, ValueError):
            threshold = 0.86
    target = _norm_concept(concept_text)
    if not target:
        return None
    try:
        import difflib
        want_scope = _scope_of(node_type, meta or {})
        best: tuple[str, float] | None = None
        for d in load_all():
            if d.get("type") != node_type:
                continue
            if _scope_of(node_type, d.get("meta") or {}) != want_scope:
                continue
            cand = _concept_of(d)
            if not cand:
                continue
            if cand == target:
                return d.get("id")
            r = difflib.SequenceMatcher(None, target, cand).ratio()
            if r >= threshold and (best is None or r > best[1]):
                best = (d.get("id"), r)
        return best[0] if best else None
    except Exception:  # noqa: BLE001 — lookup must never break a write
        return None


def dedupe_nodes() -> dict:
    """Remove DUPLICATE OKR nodes — same type + same SCOPE + same-or-NEAR
    content. Keeps the first (lowest id), deletes the rest. Matches EXACTLY on
    normalized content and FUZZILY (difflib >= AIFORGE_OKF_CONCEPT_SIMILARITY,
    default 0.86) so paraphrases of one concept — the L-01/L-07/L-13 pile-up the
    learner produced over repeated runs — collapse to a single file, restoring
    OKF 'one concept = one file'. Returns {ok, removed, kept}. Soft-fail.

    Local work on local files, on every machine: it only ever collapses nodes
    this machine minted — ``tombstone.mark_deleted`` refuses another origin — so
    there is nothing here for the admin to arbitrate. The cross-machine merge is
    the separate, admin-only step (``okf.tiers.distil_mesh``)."""
    import difflib
    import os

    from aiforge_core.memory.sync import tombstone as _tomb  # lazy: heavy package

    try:
        threshold = float(os.environ.get("AIFORGE_OKF_CONCEPT_SIMILARITY", "0.86"))
    except (TypeError, ValueError):
        threshold = 0.86
    # bucket kept concepts by (type, scope) so fuzzy compares stay in-scope
    kept: dict[tuple, list[str]] = {}
    removed = 0
    try:
        nodes = sorted(load_all(), key=lambda d: str(d.get("id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    for d in nodes:
        key_text = _concept_of(d)
        if not key_text:
            continue
        bucket = (d.get("type"), _scope_label_from_path(d.get("path", "")))
        prior = kept.setdefault(bucket, [])
        dup = key_text in prior or any(
            difflib.SequenceMatcher(None, key_text, p).ratio() >= threshold
            for p in prior)
        if dup:
            try:
                os.unlink(d["path"])
                removed += 1
            except OSError:
                continue
            # The loser is gone here, so say so to the mesh — an unlink alone is
            # undone by the next pull from any peer still holding it. Only the
            # loser: the survivor keeps its identity, and mark_deleted refuses
            # anything this machine did not mint (see there).
            m = d.get("meta") or {}
            _tomb.mark_deleted(m.get("origin"), d.get("id"), m.get("rev"))
        else:
            prior.append(key_text)
    if removed:
        _invalidate()
        _write_index()
    return {"ok": True, "removed": removed, "kept": sum(len(v) for v in kept.values())}


__all__ = ["okf_root", "type_dir", "next_id", "save_node", "read_node",
           "load_all", "okr_scopes", "migrate_scoped", "dedupe_nodes",
           "find_by_concept", "fold_session_scopes_to_global"]
