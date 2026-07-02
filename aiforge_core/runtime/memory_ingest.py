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

import logging
import os
import re
import urllib.request
from pathlib import Path

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


def _write(text: str, *, kind: str, repo: str, ref: str) -> bool:
    from aiforge_core.runtime.tools.memory_write import memory_write
    res = memory_write(text=text, kind=kind, tags=["ingest", ref], repo=repo,
                       source="ingest")
    # Count only real inserts. A deduped write returns ok=True but id=0
    # (embedded) or deduped=True (Neo4j) and persists nothing — counting it
    # made a re-index of an unchanged repo report its full unit count while
    # inserting zero rows.
    return bool(res.get("id")) and not res.get("deduped")


def _iter_files(root: Path, exts: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS
                       and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in exts:
                yield Path(dirpath) / fn


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


def _ingest_tree(root: Path, *, repo: str, exts: set[str], kind: str) -> int:
    n = 0
    for f in _iter_files(root, exts):
        text = _read_source(f)
        if not text:
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


def _neo4j_driver_or_none():
    """Open a ``neo4j.Driver`` from env, or return None when the graph
    backend isn't the active/configured one.

    Mirrors the env pattern in ``runtime.tools.memory_write``. Returns None
    (never raises) when: the active memory backend isn't ``neo4j``, the
    ``neo4j`` driver isn't installed, no URI is configured, or the connect
    fails. Layers B (symbols) and C (graphify) are graph-only, so a None
    here makes them skip cleanly on the embedded SQLite backend.
    """
    try:
        from aiforge_core.memory import backend_select
        if backend_select.memory_backend() != "neo4j":
            return None
    except Exception:  # noqa: BLE001
        return None
    uri = os.environ.get("AIFORGE_NEO4J_URI") or os.environ.get("NEO4J_URI")
    if not uri:
        return None
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception:  # noqa: BLE001
        return None


def _stat_count(stats, key: str) -> int:
    """Pull a numeric field out of an IngestStats / dict / mock stats obj."""
    if stats is None:
        return 0
    if hasattr(stats, "as_dict"):
        try:
            stats = stats.as_dict()
        except Exception:  # noqa: BLE001
            stats = {}
    if isinstance(stats, dict):
        try:
            return int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0
    v = getattr(stats, key, 0)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _index_symbols(root: Path, repo: str) -> "tuple[int, str]":
    """Layer B — tree-sitter symbol graph into Neo4j. Java-only today."""
    from aiforge_core.indexing import treesitter_ingest as tsi
    if not tsi.TREESITTER_AVAILABLE:
        return 0, "skip:treesitter_unavailable"
    driver = _neo4j_driver_or_none()
    if driver is None:
        return 0, "skip:no_neo4j"
    try:
        stats = tsi.ingest_repo(driver, Path(root), repo_name=repo)
        seen = _stat_count(stats, "files_seen")
        n = _stat_count(stats, "symbols_written")
        if seen == 0:
            return n, "skip:no_code"
        return n, "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol index failed: %s", exc)
        return 0, f"error:{exc}"
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


def _index_graphify(root: Path, repo: str) -> "tuple[int, str]":
    """Layer C — graphify knowledge graph into Neo4j. Runs the ``graphify``
    CLI as a subprocess, then loads ``graphify-out/graph.json``."""
    import shutil
    import subprocess
    if shutil.which("graphify") is None:
        return 0, "skip:graphify_cli_absent"
    driver = _neo4j_driver_or_none()
    if driver is None:
        return 0, "skip:no_neo4j"
    try:
        timeout = int(os.environ.get("AIFORGE_GRAPHIFY_TIMEOUT_S", "600"))
    except (TypeError, ValueError):
        timeout = 600
    try:
        subprocess.run(
            ["graphify", "update", "."], cwd=str(root),
            timeout=timeout, capture_output=True,
        )
        graph_json = Path(root) / "graphify-out" / "graph.json"
        if not graph_json.exists():
            return 0, "skip:no_graph_json"
        from aiforge_core.indexing.graphify_loader import load_graphify_json
        out = load_graphify_json(driver, graph_json, repo_name=repo)
        n = int((out or {}).get("nodes_created", 0) or 0)
        return n, "ok"
    except subprocess.TimeoutExpired:
        log.warning("graphify update timed out for %s", root)
        return 0, "skip:graphify_timeout"
    except Exception as exc:  # noqa: BLE001
        log.warning("graphify index failed: %s", exc)
        return 0, f"error:{exc}"
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


def _index_repo_full(root: Path, repo: str) -> dict:
    """Full multi-layer index of a repo/directory. Every layer soft-fails
    independently; chunks (A/A2) are the guaranteed baseline. Overall
    ``error`` is set only if no chunk layer produced anything."""
    layers: dict[str, str] = {}
    code_units = doc_units = symbols = graphify_nodes = 0

    # ── Layer A — code text chunks (baseline) ──
    try:
        code_units = _ingest_tree(root, repo=repo, exts=_CODE_EXT, kind="code")
        layers["code_chunks"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("code chunk index failed: %s", exc)
        layers["code_chunks"] = f"error:{exc}"

    # ── Layer A2 — document chunks (md/pdf/docx/…) ──
    if not _flag("AIFORGE_INDEX_DOCS", True):
        layers["doc_chunks"] = "skip:disabled"
    else:
        try:
            doc_units = _ingest_tree(root, repo=repo, exts=_ALL_DOC_EXT,
                                     kind="doc")
            layers["doc_chunks"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.warning("doc chunk index failed: %s", exc)
            layers["doc_chunks"] = f"error:{exc}"

    # ── Layer B — tree-sitter symbol graph (Neo4j only) ──
    if not _flag("AIFORGE_INDEX_SYMBOLS", True):
        layers["symbols"] = "skip:disabled"
    else:
        symbols, layers["symbols"] = _index_symbols(root, repo)

    # ── Layer C — graphify knowledge graph (Neo4j only) ──
    if not _flag("AIFORGE_INDEX_GRAPHIFY", True):
        layers["graphify"] = "skip:disabled"
    else:
        graphify_nodes, layers["graphify"] = _index_graphify(root, repo)

    units = code_units + doc_units
    error = None
    if units == 0 and not any(layers.get(k) == "ok"
                              for k in ("code_chunks", "doc_chunks")):
        error = "all chunk layers failed: " + \
            f"code={layers.get('code_chunks')} doc={layers.get('doc_chunks')}"
    log.info("repo index %r: units=%d symbols=%d graphify=%d layers=%s",
             repo, units, symbols, graphify_nodes, layers)
    return {"units": units, "code_units": code_units, "doc_units": doc_units,
            "symbols": symbols, "graphify_nodes": graphify_nodes,
            "layers": layers, "error": error}


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
            return _index_repo_full(root, repo)
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
