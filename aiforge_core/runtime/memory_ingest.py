"""Ingest a registered memory source into the embedded SQLite memory store.

Writes each chunk via
:func:`aiforge_core.runtime.tools.memory_write.memory_write`.
Runs in a background thread (see the API endpoint).

Kinds:
  repo  — walk a code folder/repo, chunk source files
  docs  — walk a folder for markdown/text docs
  file  — a single uploaded file
  url   — fetch a web page, strip tags
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from pathlib import Path

_SKIP_DISABLED = 'skip:disabled'

log = logging.getLogger("aiforge.memory_ingest")

_CHUNK = 1500          # chars per chunk
_MAX_CHUNKS = 4000     # safety cap per source
_MAX_FILE = 400_000    # skip files larger than this (bytes)

_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb",
    ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift", ".scala",
    ".sh", ".sql", ".yaml", ".yml", ".vue", ".css", ".scss",
}
# Text-readable document files (read straight off disk).
_DOC_EXT = {".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".csv"}
# Binary document files that need a text-extraction pass (pypdf / python-docx,
# via chat_media.extract_text). Soft-skipped if the extractor/dep is absent.
_BINARY_DOC_EXT = {".pdf", ".docx"}
# Everything the "docs" layer of a repo/dir walk considers.
_ALL_DOC_EXT = _DOC_EXT | _BINARY_DOC_EXT
_NOISE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
    ".aiforge-worktrees", "target", ".next", ".cache", "vendor", "graphify-out",
    ".pytest_cache", "site-packages",
}


def _flag(name: str, default: bool) -> bool:
    """Read a boolean env toggle. Unset -> ``default``; ``0/false/no/off/``
    (case-insensitive) -> False; anything else -> True."""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


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


def _write(text: str, *, kind: str, repo: str, ref: str,
           embed_vec: "list[float] | None" = None) -> bool:
    from aiforge_core.runtime.tools.memory_write import memory_write
    res = memory_write(text=text, kind=kind, tags=["ingest", ref], repo=repo,
                       source="ingest", embed_vec=embed_vec)
    # Count only real inserts. A deduped write returns ok=True but id=0 and
    # persists nothing — counting it made a re-index of an unchanged repo
    # report its full unit count while inserting zero rows.
    return bool(res.get("id")) and not res.get("deduped")


def _iter_files(root: Path, exts: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in exts:
                yield Path(dirpath) / fn


def _count_indexable(p: Path, sample: list) -> tuple[int, int]:
    """Capped counts of code and doc files under ``p``; fills ``sample`` with up
    to 8 relative code-file paths. Capped at 5000 each — a clear signal, not a
    full census, so a huge tree doesn't stall the pre-flight."""
    code = doc = 0
    for f in _iter_files(p, _CODE_EXT):
        code += 1
        if len(sample) < 8:
            sample.append(str(f.relative_to(p)))
        if code >= 5000:
            break
    for _ in _iter_files(p, _ALL_DOC_EXT):
        doc += 1
        if doc >= 5000:
            break
    return code, doc


def _validate_path_target(p: Path, resolved: str, out: dict) -> str | None:
    """Fill the exists/is_dir/readable flags; return an error message when the
    target cannot be indexed, or None when it is a readable directory."""
    out["exists"] = p.exists()
    if not p.exists():
        return (f"path does not exist (resolved to {resolved}). Use an ABSOLUTE "
                f"path to the repo root; a relative path resolves against the "
                f"api's working directory.")
    out["is_dir"] = p.is_dir()
    if not p.is_dir():
        return f"not a directory: {resolved}"
    out["readable"] = os.access(str(p), os.R_OK)
    return None


def validate_path(location: str) -> dict:
    """Pre-flight a repo/dir path BEFORE indexing so the user can see whether
    the process can actually reach it. Returns the RESOLVED absolute path (what
    the walk will use), whether it exists / is a dir / is readable, and how many
    code + doc files are under it — so a wrong/empty/relative path is caught up
    front instead of silently indexing 0 units. Never raises."""
    out = {"input": location, "ok": False, "resolved": "", "exists": False,
           "is_dir": False, "readable": False, "code_files": 0, "doc_files": 0,
           "sample": [], "message": ""}
    try:
        loc = (location or "").strip()
        if not loc:
            out["message"] = "empty path"
            return out
        p = Path(loc).expanduser()
        out["resolved"] = str(p.resolve()) if p.exists() else str(p.absolute())
        err = _validate_path_target(p, out["resolved"], out)
        if err:
            out["message"] = err
            return out
        code, doc = _count_indexable(p, out["sample"])
        out["code_files"], out["doc_files"] = code, doc
        if code == 0 and doc == 0:
            out["message"] = (f"0 indexable files under {out['resolved']} — the "
                              f"directory is empty from the api's view. On a "
                              f"HYBRID/host run give the ABSOLUTE host path to the "
                              f"repo root (the dir with src/ or pom.xml); on Docker "
                              f"mount it under /workspace.")
        else:
            out["ok"] = True
            out["message"] = (f"OK — {code} code + {doc} doc files under "
                              f"{out['resolved']}")
    except Exception as exc:  # noqa: BLE001
        out["message"] = f"validation error: {exc}"
    return out


def _read_source(f: Path) -> "str | None":
    """Read a file to text. Binary docs (pdf/docx) go through
    ``chat_media.extract_text`` (pypdf / python-docx); text files are read
    straight off disk. Returns None on any failure (soft-skip)."""
    ext = f.suffix.lower()
    if ext in _BINARY_DOC_EXT:
        try:
            from aiforge_core.runtime import chat_media
            text = chat_media.extract_text(str(f))
        except Exception:  # noqa: BLE001 — missing dep / corrupt file
            return None
        return text or None
    try:
        if f.stat().st_size > _MAX_FILE:
            return None
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


_EMBED_BATCH = 32  # docs per /embed_batch call — CPU bge-m3 batches far faster
                   # than one-at-a-time (a single doc is ~2s; a 32-doc batch is
                   # nowhere near 32×), so a big repo indexes in minutes not hours.


def _collect_chunks(root: Path, exts: set[str]) -> list[tuple[str, str]]:
    """(chunk_text, rel_ref) for every source file under ``root``, each chunk
    prefixed with a ``# <rel>`` header. Bounded by ``_MAX_CHUNKS`` so a huge tree
    can't blow memory."""
    pending: list[tuple[str, str]] = []
    for f in _iter_files(root, exts):
        text = _read_source(f)
        if not text:
            continue
        rel = str(f.relative_to(root))
        header = f"# {rel}\n"
        for ch in _chunks(text):
            if len(pending) >= _MAX_CHUNKS:
                return pending
            pending.append((header + ch, rel))
    return pending


def _embed_batch_vecs(batch: list[tuple[str, str]]) -> "list[list[float] | None]":
    """Embed one batch in a single round-trip; soft-fail to per-write embed (all
    None) so a sidecar hiccup never aborts the ingest."""
    try:
        from aiforge_core.memory.embed import embed_batch as _eb
        return _eb([t for t, _ in batch])  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        log.warning("batch embed failed (%s); falling back per-write", exc)
        return [None] * len(batch)


def _ingest_tree(root: Path, *, repo: str, exts: set[str], kind: str) -> int:
    # Collect (chunk_text, ref) first so we can EMBED IN BATCHES, then write.
    pending = _collect_chunks(root, exts)
    n = 0
    for i in range(0, len(pending), _EMBED_BATCH):
        batch = pending[i:i + _EMBED_BATCH]
        for (txt, rel), vec in zip(batch, _embed_batch_vecs(batch)):
            if _write(txt, kind=kind, repo=repo, ref=rel, embed_vec=vec):
                n += 1
    return n


_TAG_RE = re.compile(r"<[^>]+>")


def _fetch_url(url: str) -> str:
    # SSRF guard: this is an UNAUTHENTICATED ``kind=url`` ingest source, so a
    # caller must not be able to make the server fetch cloud metadata
    # (169.254.169.254), loopback services or the private LAN. A DNS failure
    # is left to urlopen to surface naturally.
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            raise
    req = urllib.request.Request(url, headers={"User-Agent": "aiforge-ingest"})
    # Arbitrary user-supplied ingest source — keep stdlib default TLS
    # verification (the AIFORGE_LLM_SSL_VERIFY opt-out is internal-only).
    with urllib.request.urlopen(req, timeout=20) as r:
        # Re-guard the post-redirect URL before consuming the body.
        final = getattr(r, "url", None)
        if final and final != url:
            try:
                guard_public_url(final)
            except SSRFBlocked as exc:
                if exc.kind != "dns":
                    raise
        raw = r.read().decode("utf-8", errors="replace")
    # crude HTML strip
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw,
                 flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+\n", "\n", _TAG_RE.sub(" ", raw))


def _index_chunk_layer(root: Path, repo: str, flag: str, exts: set, kind: str,
                       layers: dict, layer_key: str) -> int:
    """Run one chunk layer (code or doc). Records its status in ``layers`` and
    returns the units written. Skipped when its flag is off; soft-fails."""
    if not _flag(flag, True):
        layers[layer_key] = _SKIP_DISABLED
        return 0
    try:
        units = _ingest_tree(root, repo=repo, exts=exts, kind=kind)
        layers[layer_key] = "ok"
        return units
    except Exception as exc:  # noqa: BLE001
        log.warning("%s index failed: %s", layer_key, exc)
        layers[layer_key] = f"error:{exc}"
        return 0


def _empty_index_error(root: Path, layers: dict) -> "str | None":
    """The error message for a walk that produced nothing anywhere (almost always
    a wrong / empty / unmounted path). Surfaces the RESOLVED abs path so the user
    sees where the indexer actually looked. None when a chunk layer errored (a
    real failure, reported separately)."""
    if any(str(layers.get(k, "")).startswith("error:")
           for k in ("code_chunks", "doc_chunks")):
        return None
    try:
        abs_path = str(root.resolve())
    except Exception:  # noqa: BLE001
        abs_path = str(root)
    layers["code_chunks"] = "skip:no_files"
    return (f"indexed 0 files from {abs_path} — the directory is empty from the "
            f"indexer's view. Check the path is correct and, if running the api "
            f"in Docker, that this repo is under the mounted workspace "
            f"(AIFORGE_HOST_WORKSPACE → /workspace).")


def _index_repo_full(root: Path, repo: str) -> dict:
    """Full index of a repo/directory into the embedded SQLite store. The code
    + doc chunk layers soft-fail independently and are the guaranteed baseline.
    Overall ``error`` is set only if no chunk layer produced anything.
    """
    layers: dict[str, str] = {}
    code_units = _index_chunk_layer(root, repo, "AIFORGE_INDEX_CODE_CHUNKS",
                                    _CODE_EXT, "code", layers, "code_chunks")
    doc_units = _index_chunk_layer(root, repo, "AIFORGE_INDEX_DOCS",
                                   _ALL_DOC_EXT, "doc", layers, "doc_chunks")

    units = code_units + doc_units
    error = None
    if units == 0:
        error = _empty_index_error(root, layers)
    if error is None and units == 0 and not any(
            layers.get(k) == "ok" for k in ("code_chunks", "doc_chunks")):
        error = ("all chunk layers failed: "
                 f"code={layers.get('code_chunks')} doc={layers.get('doc_chunks')}")
    log.info("repo index %r: units=%d layers=%s", repo, units, layers)
    return {"units": units, "code_units": code_units, "doc_units": doc_units,
            "symbols": 0, "graphify_nodes": 0,
            "layers": layers, "error": error}


def _ingest_dir(loc: str, repo: str, exts: set, kind: str) -> dict:
    """Ingest a directory tree, or an error dict when it is not a directory."""
    root = Path(loc).expanduser()
    if not root.is_dir():
        return {"units": 0, "error": f"not a directory: {loc}"}
    return {"units": _ingest_tree(root, repo=repo, exts=exts, kind=kind),
            "error": None}


def _ingest_single_file(loc: str, repo: str) -> dict:
    """Ingest one file as doc chunks, or an error dict when it is not a file."""
    f = Path(loc).expanduser()
    if not f.is_file():
        return {"units": 0, "error": f"not a file: {loc}"}
    text = f.read_text(encoding="utf-8", errors="replace")
    n = sum(1 for ch in _chunks(text)
            if _write(f"# {f.name}\n" + ch, kind="doc", repo=repo, ref=f.name))
    return {"units": n, "error": None}


def _ingest_url(loc: str, repo: str) -> dict:
    """Fetch + ingest a URL's text as doc chunks."""
    text = _fetch_url(loc)
    n = sum(1 for ch in _chunks(text)
            if _write(ch, kind="doc", repo=repo, ref=loc))
    return {"units": n, "error": None}


def ingest_source(source: dict) -> dict:
    """Ingest one source dict ({kind, name, location}). Returns
    ``{units, error}``. Never raises — errors are returned."""
    kind = source["kind"]
    # Key by the BARE repo name (strip the ' (Python)' display suffix) so indexed
    # chunks/symbols file under the SAME key the chat/recall path uses (git
    # basename) — else recall by "requests" never finds "requests (Python)".
    from aiforge_core.runtime.repo_ident import normalize_repo as _nr
    repo = _nr(source.get("name") or "") or "memory"
    loc = source["location"]
    try:
        if kind == "repo":
            root = Path(loc).expanduser()
            if not root.is_dir():
                return {"units": 0, "error": f"not a directory: {loc}"}
            return _index_repo_full(root, repo)
        if kind == "docs":
            return _ingest_dir(loc, repo, _DOC_EXT, "doc")
        if kind == "file":
            return _ingest_single_file(loc, repo)
        if kind == "url":
            return _ingest_url(loc, repo)
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
    # Heartbeat the lease while the (potentially long, blocking) ingest runs so
    # the stale-index reaper (AIFORGE_INDEX_LEASE_S) only reaps a genuinely
    # STALLED index — not a slow-but-progressing one. Big repos on slow
    # filesystems (e.g. WSL /mnt/c) legitimately exceed the default lease and
    # would otherwise be reset to idle mid-run ("indexing exceeded lease") in a
    # loop that never finishes. A crashed ingest stops the beat → still reaped.
    import threading
    _stop = threading.Event()
    _hb_s = max(15, int(os.environ.get("AIFORGE_INDEX_HEARTBEAT_S", "60")))
    def _heartbeat() -> None:
        while not _stop.wait(_hb_s):
            try:
                _ms.touch_indexing(source_id)
            except Exception:  # noqa: BLE001 — heartbeat must never crash ingest
                pass
    threading.Thread(target=_heartbeat, name=f"index-hb-{source_id}",
                     daemon=True).start()
    try:
        res = ingest_source(source)
    except Exception as exc:  # noqa: BLE001 — never leave the row stuck 'indexing'
        log.warning("run_index crashed for source %s: %s", source_id, exc)
        try:
            _ms.set_status(source_id, "error", units=0, error=str(exc))
        except Exception:  # noqa: BLE001
            pass
        return
    finally:
        _stop.set()
    layers = res.get("layers")
    # A layer that errored (e.g. symbols=error:… while chunks=ok) must not
    # be reported as a clean "done" — surface it as "partial" and carry the
    # per-layer detail so the operator sees which layer failed.
    failed = {}
    if isinstance(layers, dict):
        failed = {k: v for k, v in layers.items()
                  if isinstance(v, str) and v.startswith("error:")}
    if res.get("error"):
        _ms.set_status(source_id, "error", units=res.get("units", 0),
                       error=res["error"], layers=layers)
    elif failed:
        detail = "partial index — " + "; ".join(
            f"{k}={v}" for k, v in failed.items())
        _ms.set_status(source_id, "partial", units=res.get("units", 0),
                       error=detail, layers=layers, indexed=True)
    else:
        _ms.set_status(source_id, "done", units=res.get("units", 0),
                       error=None, layers=layers, indexed=True)


def _unchanged_since_last_index(location: str) -> bool:
    """True when NOTHING under ``location`` changed since we last indexed it
    (merkle content-hash tree). Lets the daily sweep SKIP a full rebuild of an
    untouched repo — a full index is minutes of LLM-summary + CPU-embed, so
    re-running it daily on an unchanged repo is pure waste. Soft-fails to False
    (index anyway) so a merkle glitch never SKIPS a real change."""
    try:
        from aiforge_core.indexing import merkle
        prev = merkle.current_root(location)
        if prev is None:
            return False                     # never indexed → must index
        changed = merkle.diff(location, prev)
        return not changed
    except Exception:  # noqa: BLE001
        return False


def reindex_all(*, force: bool = False) -> dict:
    """Re-index every registered repo/docs source whose content CHANGED since the
    last index (merkle diff) — an unchanged repo is skipped, so the daily sweep
    doesn't pay the minutes-long full rebuild for nothing. ``force=True`` rebuilds
    all. Sequential + soft-fail per source. Returns
    ``{total, indexed, skipped, errors}``."""
    from aiforge_core.runtime import memory_sources as _ms
    out = {"total": 0, "indexed": 0, "skipped": 0, "errors": []}
    try:
        sources = _ms.list_sources()
    except Exception as exc:  # noqa: BLE001
        return {**out, "errors": [{"id": None, "error": str(exc)}]}
    for s in sources:
        if s.get("kind") not in ("repo", "docs"):
            continue
        out["total"] += 1
        sid = s.get("id")
        loc = s.get("location") or ""
        if not force and loc and _unchanged_since_last_index(loc):
            out["skipped"] += 1
            continue
        try:
            _ms.set_status(sid, "indexing", error=None)
            run_index(sid)                     # in-process (caller is off the API loop)
            try:                               # refresh the merkle baseline
                from aiforge_core.indexing import merkle
                merkle.build(loc)
            except Exception:  # noqa: BLE001
                pass
            out["indexed"] += 1
        except Exception as exc:  # noqa: BLE001 — never let one repo stop the sweep
            log.warning("reindex_all: source %s failed: %s", sid, exc)
            out["errors"].append({"id": sid, "error": str(exc)})
    log.info("reindex_all: %d indexed, %d skipped (unchanged), %d errors / %d total",
             out["indexed"], out["skipped"], len(out["errors"]), out["total"])
    return out


if __name__ == "__main__":  # subprocess entrypoint: `python -m … <source_id>`
    # Indexing is CPU-bound (tree-sitter parsing, chunking) and holds the GIL
    # for long stretches; running it in an api THREAD starves uvicorn's asyncio
    # loop and wedges every request (health, the UI, the public tunnel). The
    # api dispatches this module as a SEPARATE PROCESS so its GIL is its own.
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        # Daily sweep: re-index every registered repo/docs source. Own process
        # so the CPU-heavy work never touches the API's GIL.
        r = reindex_all()
        raise SystemExit(0 if not r.get("errors") else 1)
    raise SystemExit(0 if (len(sys.argv) > 1 and run_index(int(sys.argv[1])) is None)
                     else 1)
