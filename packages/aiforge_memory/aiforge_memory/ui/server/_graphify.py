"""Graphify-out discovery helpers (filesystem walk + path resolution)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# Lazy-import fastapi so the rest of the package works without it.
# _resolve_graphify_path raises HTTPException, but is only ever called from
# within the FastAPI request handlers (so fastapi is present at call time).
try:
    from fastapi import HTTPException
except ImportError:
    pass


def _graphify_extra_roots() -> list[Path]:
    """Repos that aren't in scheduler.yaml but should still be surfaced.

    AIForgeCrew (the orchestrator) keeps its own graphify-out beside the
    AiForgeMemory checkout — so the UI can browse the meta-graph even
    before the orchestrator repo is registered for ingest.
    """
    candidates = [
        Path.home() / "AIForgeCrew",
        Path.home() / "Documents" / "codeRepo" / "AIForgeCrew",
        Path.home() / "codeRepo" / "AIForgeCrew",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for c in candidates:
        if c.is_dir() and (c / "graphify-out").is_dir():
            key = str(c.resolve())
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def _graphify_metadata(repo_name: str, repo_path: Path) -> dict | None:
    """Inspect <repo_path>/graphify-out/ and return descriptor, or None.

    Cheap stat-only walk — no file content parsed beyond graph.json's nodes
    array (read once, length only). Resilient to partial outputs.
    """
    gdir = repo_path / "graphify-out"
    if not gdir.is_dir():
        return None

    report = gdir / "GRAPH_REPORT.md"
    graph_json = gdir / "graph.json"
    wiki_md = gdir / "wiki" / "index.md"
    wiki_html = gdir / "wiki" / "index.html"

    has_report = report.is_file()
    has_graph_json = graph_json.is_file()
    has_wiki = wiki_md.is_file() or wiki_html.is_file()

    node_count = 0
    if has_graph_json:
        try:
            with graph_json.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
            nodes = doc.get("nodes")
            if isinstance(nodes, list):
                node_count = len(nodes)
        except (OSError, ValueError):
            node_count = 0

    # Total bytes (recursive) — cheap because graphify-out is typically <100MB.
    total_bytes = 0
    for p in gdir.rglob("*"):
        try:
            if p.is_file():
                total_bytes += p.stat().st_size
        except OSError:
            continue

    # mtime preference: graph.json → GRAPH_REPORT.md → directory.
    mtime_src: Path | None = None
    if has_graph_json:
        mtime_src = graph_json
    elif has_report:
        mtime_src = report
    else:
        mtime_src = gdir
    try:
        mt = mtime_src.stat().st_mtime
        updated_at = datetime.fromtimestamp(mt, tz=UTC) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
    except OSError:
        updated_at = ""

    return {
        "repo": repo_name,
        "path": str(gdir),
        "has_report": has_report,
        "has_graph_json": has_graph_json,
        "has_wiki": has_wiki,
        "node_count": node_count,
        "size_kb": total_bytes // 1024,
        "updated_at": updated_at,
    }


def _graphify_index() -> dict[str, dict]:
    """Discover all repos with a graphify-out dir.

    Sources (deduped by repo name, scheduler entries take precedence):
      1. ~/.aiforge/scheduler.yaml registered repos
      2. Hard-coded extra roots (AIForgeCrew orchestrator)

    Returns a {name: descriptor} mapping. The descriptor's `path` field is
    the absolute path of `<repo>/graphify-out/` — the only directory the
    HTTP layer is allowed to serve from.
    """
    out: dict[str, dict] = {}

    # 1. Scheduler-registered repos.
    try:
        from aiforge_memory.features.scheduler import runner as sched
        cfg = sched.SchedulerConfig.load()
        for r in cfg.repos:
            try:
                rp = Path(r.path).expanduser()
            except (TypeError, ValueError):
                continue
            md = _graphify_metadata(r.name, rp)
            if md:
                out[r.name] = md
    except Exception:  # noqa: BLE001 — scheduler import is best-effort
        pass

    # 2. AIForgeCrew (orchestrator self-graph).
    for extra in _graphify_extra_roots():
        if "AIForgeCrew" in out:
            continue
        md = _graphify_metadata("AIForgeCrew", extra)
        if md:
            out["AIForgeCrew"] = md

    return out


def _resolve_graphify_path(repo: str, *parts: str) -> Path:
    """Resolve a path inside <repo>/graphify-out/ with traversal protection.

    Raises HTTPException(404) for unknown repos and HTTPException(400) for
    any path that escapes the graphify-out subtree (e.g. `..` segments,
    absolute paths, symlinks aimed outside).
    """
    # Route through the package object so callers that monkeypatch
    # ``aiforge_memory.ui.server._graphify_index`` (the historical single-module
    # attribute) still influence resolution after the split.
    from aiforge_memory.ui import server as _pkg
    idx = _pkg._graphify_index()
    desc = idx.get(repo)
    if not desc:
        raise HTTPException(404, f"unknown graphify repo: {repo}")
    base = Path(desc["path"]).resolve()
    if not parts:
        return base
    # Reject obvious escape attempts before resolve() collapses them.
    for p in parts:
        if not p or p.startswith("/") or ".." in Path(p).parts:
            raise HTTPException(400, "invalid path")
    target = (base.joinpath(*parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(400, "path escapes graphify-out") from exc
    return target
