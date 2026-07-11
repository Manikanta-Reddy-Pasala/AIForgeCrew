"""OKR node store — the typed folder layout under ``<memory>/okr/``.

    objectives/  key_results/  learnings/  sessions/

Save/load nodes, allocate the next id per type, enumerate the graph. Folder is
created lazily; all reads soft-fail (a missing/ half-written file is skipped).
"""
from __future__ import annotations

import os
import re

from . import nodes as _n

# type → folder name.
_DIR = {"objective": "objectives", "key_result": "key_results",
        "learning": "learnings", "session": "sessions"}


def okr_root() -> str:
    from aiforge_core.memory.md_store import memory_dir
    return os.path.join(str(memory_dir()), "okr")


def type_dir(node_type: str) -> str:
    d = os.path.join(okr_root(), _DIR.get(node_type, "misc"))
    os.makedirs(d, exist_ok=True)
    return d


def _filename(node_type: str, node_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(node_id)).strip("-") or "node"
    return f"{safe}.md"


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
    d = os.path.join(okr_root(), _DIR.get(node_type, "misc"))
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]


def save_node(node_type: str, node_id: str | None, meta: dict,
              body: str = "") -> dict:
    """Render + write a node atomically. ``node_id=None`` allocates one.
    Returns ``{ok, id, path}`` (soft-fail)."""
    if node_type not in _n.NODE_TYPES:
        return {"ok": False, "error": f"unknown type {node_type!r}"}
    nid = str(node_id or next_id(node_type))
    path = os.path.join(type_dir(node_type), _filename(node_type, nid))
    text = _n.render_node(node_type, nid, meta, body)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return {"ok": False, "error": f"write failed: {exc}"}
    _write_index()   # keep the reserved OKF navigation file fresh
    return {"ok": True, "id": nid, "path": path}


def _write_index() -> str:
    """(Re)generate the reserved OKF ``index.md`` at the bundle root — pure
    navigation, NO frontmatter (spec §3). Lists every concept grouped by folder,
    with absolute bundle-relative links. Soft-fail (best-effort)."""
    try:
        from aiforge_core.memory import okf
        root = okr_root()
        entries: list[tuple[str, str]] = []
        for d in load_all():
            p = d.get("path", "")
            rel = "/" + os.path.relpath(p, root).replace(os.sep, "/")
            meta = d.get("meta") or {}
            hook = (meta.get("title") or meta.get("description")
                    or (d.get("body") or "").strip().split("\n", 1)[0])[:80]
            entries.append((rel, hook))
        entries.sort()
        text = okf.render_index("OKR Knowledge Bundle", entries)
        idx = os.path.join(root, "index.md")
        with open(idx, "w", encoding="utf-8") as fh:
            fh.write(text)
        return idx
    except Exception:  # noqa: BLE001 — index is navigation, never block a save
        return ""


def read_node(node_type: str, node_id: str) -> dict | None:
    path = os.path.join(okr_root(), _DIR.get(node_type, "misc"),
                        _filename(node_type, node_id))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = _n.parse_node(fh.read())
        d["path"] = path
        return d
    except OSError:
        return None


def load_all() -> list[dict]:
    """Every OKR node across all four folders (parsed). Skips unreadable files.
    Each dict is ``{type, id, meta, body, path}``."""
    out: list[dict] = []
    for t in _n.NODE_TYPES:
        for f in _list_files(t):
            try:
                with open(f, encoding="utf-8") as fh:
                    d = _n.parse_node(fh.read())
                # trust the folder for type when the frontmatter omits it
                if not d.get("type"):
                    d["type"] = t
                if not d.get("id"):
                    d["id"] = os.path.splitext(os.path.basename(f))[0]
                d["path"] = f
                out.append(d)
            except OSError:
                continue
    return out


__all__ = ["okr_root", "type_dir", "next_id", "save_node", "read_node",
           "load_all"]
