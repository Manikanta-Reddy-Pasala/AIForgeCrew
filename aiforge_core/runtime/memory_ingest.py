"""Ingest a registered memory source into the active memory backend.

Backend-agnostic: writes each chunk via
:func:`aiforge_core.runtime.tools.memory_write.memory_write`, which
already routes to embedded SQLite or Neo4j depending on the active
backend. Runs in a background thread (see the API endpoint).

Kinds:
  repo  — walk a code folder/repo, chunk source files
  docs  — walk a folder for markdown/text docs
  file  — a single uploaded file
  url   — fetch a web page, strip tags
"""
from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path

_CHUNK = 1500          # chars per chunk
_MAX_CHUNKS = 4000     # safety cap per source
_MAX_FILE = 400_000    # skip files larger than this (bytes)

_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb",
    ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift", ".scala",
    ".sh", ".sql", ".yaml", ".yml", ".vue", ".css", ".scss",
}
_DOC_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc"}
_NOISE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
    ".aiforge-worktrees", "target", ".next", ".cache", "vendor", "graphify-out",
    ".pytest_cache", "site-packages",
}


def _chunks(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    out, buf, size = [], [], 0
    for line in text.splitlines(keepends=True):
        buf.append(line)
        size += len(line)
        if size >= _CHUNK:
            out.append("".join(buf))
            buf, size = [], 0
    if buf:
        out.append("".join(buf))
    return out


def _write(text: str, *, kind: str, repo: str, ref: str) -> bool:
    from aiforge_core.runtime.tools.memory_write import memory_write
    res = memory_write(text=text, kind=kind, tags=["ingest", ref], repo=repo)
    return bool(res.get("ok"))


def _iter_files(root: Path, exts: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in exts:
                yield Path(dirpath) / fn


def _ingest_tree(root: Path, *, repo: str, exts: set[str], kind: str) -> int:
    n = 0
    for f in _iter_files(root, exts):
        try:
            if f.stat().st_size > _MAX_FILE:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root))
        header = f"# {rel}\n"
        for ch in _chunks(text):
            if n >= _MAX_CHUNKS:
                return n
            if _write(header + ch, kind=kind, repo=repo, ref=rel):
                n += 1
    return n


_TAG_RE = re.compile(r"<[^>]+>")


def _fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "aiforge-ingest"})
    # Arbitrary user-supplied ingest source — keep stdlib default TLS
    # verification (the AIFORGE_LLM_SSL_VERIFY opt-out is internal-only).
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", errors="replace")
    # crude HTML strip
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw,
                 flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+\n", "\n", _TAG_RE.sub(" ", raw))


def ingest_source(source: dict) -> dict:
    """Ingest one source dict ({kind, name, location}). Returns
    ``{units, error}``. Never raises — errors are returned."""
    kind = source["kind"]
    repo = source.get("name") or "memory"
    loc = source["location"]
    try:
        if kind == "repo":
            root = Path(loc).expanduser()
            if not root.is_dir():
                return {"units": 0, "error": f"not a directory: {loc}"}
            return {"units": _ingest_tree(root, repo=repo,
                                          exts=_CODE_EXT | _DOC_EXT, kind="code"),
                    "error": None}
        if kind == "docs":
            root = Path(loc).expanduser()
            if not root.is_dir():
                return {"units": 0, "error": f"not a directory: {loc}"}
            return {"units": _ingest_tree(root, repo=repo, exts=_DOC_EXT,
                                          kind="doc"), "error": None}
        if kind == "file":
            f = Path(loc).expanduser()
            if not f.is_file():
                return {"units": 0, "error": f"not a file: {loc}"}
            text = f.read_text(encoding="utf-8", errors="replace")
            n = sum(1 for ch in _chunks(text)
                    if _write(f"# {f.name}\n" + ch, kind="doc", repo=repo,
                              ref=f.name))
            return {"units": n, "error": None}
        if kind == "url":
            text = _fetch_url(loc)
            n = sum(1 for ch in _chunks(text)
                    if _write(ch, kind="doc", repo=repo, ref=loc))
            return {"units": n, "error": None}
        return {"units": 0, "error": f"unknown kind: {kind}"}
    except Exception as exc:  # noqa: BLE001
        return {"units": 0, "error": str(exc)}


def run_index(source_id: int) -> None:
    """Background entrypoint: ingest a source by id, updating its status."""
    from aiforge_core.runtime import memory_sources as _ms
    source = _ms.get(source_id)
    if not source:
        return
    _ms.set_status(source_id, "indexing", error=None)
    res = ingest_source(source)
    if res.get("error"):
        _ms.set_status(source_id, "error", units=res.get("units", 0),
                       error=res["error"])
    else:
        _ms.set_status(source_id, "done", units=res.get("units", 0),
                       error=None, indexed=True)
