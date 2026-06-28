"""Minimal read-only UI for AiForgeMemory.

Single-file FastAPI server + embedded HTML/JS. No auth — bind to
localhost or trusted LAN. Five things visible:

    /                  Dashboard (repos, scheduler, health)
    /api/repos         List repos + counts
    /api/health        Sidecar status (calls ops.health)
    /api/scheduler     Daemon + per-repo last_run/status
    /api/search        NL search → ContextBundle (any repo)
    /api/memory        Memory nodes (decision/observation/note/doc)
    /api/repo/{name}   Repo detail (recent files + summaries)
    /api/file          File detail by path (chunks + symbols)

Run:
    aiforge-memory ui [--host 0.0.0.0] [--port 8767]

Requires fastapi + uvicorn (added to optional `[ui]` extra).
"""
from __future__ import annotations

import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

# Lazy-import fastapi so the rest of the package works without it.
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
        PlainTextResponse,
        Response,
    )
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


HTML_PATH = Path(__file__).parent / "index.html"

# Cap on /api/graphify/{repo}/graph payload — anything bigger and the JSON
# materialisation stalls the UI. Operator should download graph.json directly.
_GRAPHIFY_JSON_CAP_BYTES = 50 * 1024 * 1024


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
    idx = _graphify_index()
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


def build_app():
    if not _HAS_FASTAPI:
        raise RuntimeError(
            "fastapi not installed. Run: uv pip install '.[ui]'"
        )

    from aiforge_memory.core.neo4j import open_driver

    app = FastAPI(title="AiForgeMemory UI", docs_url="/docs", redoc_url=None)

    def _driver():
        return open_driver()

    # ── HTML ─────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        if HTML_PATH.is_file():
            return HTML_PATH.read_text()
        return "<h1>AiForgeMemory UI</h1><p>HTML missing.</p>"

    # ── Repos list with counts ───────────────────────────────────────

    @app.get("/api/repos")
    async def repos():
        """Per-repo counts via separate queries — avoids the cartesian
        explosion that an OPTIONAL MATCH chain produces on PCB-scale graphs."""
        drv = _driver()
        try:
            with drv.session() as s:
                meta = list(s.run("""
                    MATCH (r:Repo)
                    RETURN r.name AS name,
                           coalesce(r.lang_primary,'') AS lang,
                           coalesce(r.head_sha,'') AS head,
                           coalesce(r.branch,'') AS branch,
                           toString(r.last_indexed_at) AS indexed
                    ORDER BY r.name
                """))
                out: list[dict] = []
                for m in meta:
                    n = m["name"]
                    f = s.run(
                        "MATCH (f:File_v2 {repo:$n}) RETURN count(f) AS c", n=n,
                    ).single()["c"]
                    sym = s.run(
                        "MATCH (s:Symbol_v2 {repo:$n}) RETURN count(s) AS c", n=n,
                    ).single()["c"]
                    ch = s.run(
                        "MATCH (c:Chunk_v2 {repo:$n}) RETURN count(c) AS c", n=n,
                    ).single()["c"]
                    out.append({
                        **dict(m),
                        "files": f, "symbols": sym, "chunks": ch,
                    })
            return out
        finally:
            drv.close()

    @app.get("/api/repo/{name}")
    async def repo_detail(name: str):
        drv = _driver()
        try:
            with drv.session() as s:
                row = s.run(
                    "MATCH (r:Repo {name:$n}) RETURN r", n=name,
                ).single()
                if not row:
                    raise HTTPException(404, "repo not found")
                meta = dict(row["r"])
                # cast datetime fields to string
                for k, v in list(meta.items()):
                    if hasattr(v, "iso_format"):
                        meta[k] = str(v)
                # NULLS LAST is Cypher 25-only; older Neo4j (<5.27) refuses.
                # Coerce nulls to a fixed-old datetime via coalesce so the
                # sort works on every supported version.
                files = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$n})
                    RETURN f.path AS path, f.lang AS lang, f.lines AS lines,
                           coalesce(f.summary,'') AS summary,
                           toString(f.indexed_at) AS indexed
                    ORDER BY coalesce(f.indexed_at, datetime('1970-01-01T00:00:00Z')) DESC, f.path
                    LIMIT 200
                """, n=name)]
                services = [dict(r) for r in s.run("""
                    MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service)
                    RETURN s.name AS name, s.role AS role,
                           s.description AS description, s.port AS port,
                           s.tech_stack AS tech_stack
                """, n=name)]
            return {"meta": meta, "services": services, "files": files}
        finally:
            drv.close()

    @app.get("/api/file")
    async def file_detail(repo: str, path: str):
        drv = _driver()
        try:
            with drv.session() as s:
                row = s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p}) RETURN f
                """, r=repo, p=path).single()
                if not row:
                    raise HTTPException(404, "file not found")
                meta = dict(row["f"])
                for k, v in list(meta.items()):
                    if hasattr(v, "iso_format"):
                        meta[k] = str(v)
                symbols = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p})-[:DEFINES]->(s:Symbol_v2)
                    RETURN s.fqname AS fqname, s.kind AS kind,
                           s.signature AS signature,
                           coalesce(s.visibility,'') AS visibility,
                           coalesce(s.return_type,'') AS return_type,
                           coalesce(s.summary,'') AS summary,
                           coalesce(s.doc_first_line,'') AS doc,
                           coalesce(s.modifiers,[]) AS modifiers,
                           coalesce(s.deprecated,false) AS deprecated,
                           coalesce(s.params_json,'') AS params_json,
                           s.line_start AS line_start, s.line_end AS line_end
                    ORDER BY s.line_start
                """, r=repo, p=path)]
                # Parse params_json on the server so the UI doesn't need
                # to fight Cypher's string typing.
                import json as _json
                for sym in symbols:
                    raw = sym.pop("params_json", "")
                    try:
                        sym["params"] = _json.loads(raw) if raw else []
                    except (ValueError, TypeError):
                        sym["params"] = []
                chunks = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p})-[:CHUNKED_AS]->(c:Chunk_v2)
                    RETURN c.id AS id, c.line_start AS line_start,
                           c.line_end AS line_end, c.token_count AS tokens,
                           substring(c.text, 0, 400) AS preview
                    ORDER BY c.line_start
                """, r=repo, p=path)]
            return {"meta": meta, "symbols": symbols, "chunks": chunks}
        finally:
            drv.close()

    # ── Scheduler + health ───────────────────────────────────────────

    @app.get("/api/scheduler")
    async def scheduler_status():
        from aiforge_memory.features.scheduler import runner as sched
        return JSONResponse(sched.daemon_status())

    @app.get("/api/health")
    async def health():
        from dataclasses import asdict

        from aiforge_memory.ops import health as h
        report = h.check_all()
        return {
            "ts": report.ts,
            "overall_ok": report.overall_ok,
            "checks": [asdict(c) for c in report.checks],
        }

    # ── Search (NL → ContextBundle) ──────────────────────────────────

    @app.post("/api/search")
    async def search(payload: dict):
        from dataclasses import asdict

        from aiforge_memory.query import bundle as B
        text = (payload.get("query") or "").strip()
        repo = (payload.get("repo") or "").strip()
        if not text or not repo:
            raise HTTPException(400, "query + repo required")
        token_budget = int(payload.get("token_budget") or 4000)
        drv = _driver()
        try:
            b = B.query(text, repo=repo, driver=drv, token_budget=token_budget)
            return {
                "query": text, "repo": repo,
                "bundle": asdict(b),
                "rendered": b.render(),
            }
        finally:
            drv.close()

    # ── Memory nodes ─────────────────────────────────────────────────

    @app.get("/api/memory")
    async def memory_list(
        repo: str,
        type: str | None = Query(None,
                                 pattern="^(decision|observation|note|doc)$"),
        limit: int = 50,
    ):
        from aiforge_memory.features.memory import store as memory_writer
        label_map = {
            "decision": "Decision_v2", "observation": "Observation_v2",
            "note": "Note_v2", "doc": "Doc_v2",
        }
        label = label_map.get(type) if type else None
        drv = _driver()
        try:
            return memory_writer.list_memory(
                drv, repo=repo, label=label, limit=limit,
            )
        finally:
            drv.close()

    # ── Cross-repo ───────────────────────────────────────────────────

    @app.get("/api/links")
    async def links(repo: str | None = None):
        from aiforge_memory.features.link import store as link_writer
        drv = _driver()
        try:
            return link_writer.list_edges(drv, repo=repo)
        finally:
            drv.close()

    # ── Graphify (browse graphify-out HTML knowledge graphs) ─────────

    @app.get("/api/graphify")
    async def graphify_list():
        """All repos that have a graphify-out/ directory.

        Walks scheduler.yaml + the AIForgeCrew orchestrator dir, returning
        {repo, path, has_report, has_graph_json, has_wiki, node_count,
         size_kb, updated_at} per entry.
        """
        idx = _graphify_index()
        return sorted(idx.values(), key=lambda d: d["repo"].lower())

    @app.get("/api/graphify/{repo}/report")
    async def graphify_report(repo: str):
        target = _resolve_graphify_path(repo, "GRAPH_REPORT.md")
        if not target.is_file():
            raise HTTPException(404, f"GRAPH_REPORT.md missing for {repo}")
        return PlainTextResponse(
            target.read_text(encoding="utf-8", errors="replace"),
            media_type="text/markdown; charset=utf-8",
        )

    @app.get("/api/graphify/{repo}/graph")
    async def graphify_graph(repo: str):
        target = _resolve_graphify_path(repo, "graph.json")
        if not target.is_file():
            raise HTTPException(404, f"graph.json missing for {repo}")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise HTTPException(500, f"graph.json stat failed: {exc}") from exc
        if size > _GRAPHIFY_JSON_CAP_BYTES:
            raise HTTPException(
                413,
                f"graph.json is {size // (1024*1024)} MB (cap "
                f"{_GRAPHIFY_JSON_CAP_BYTES // (1024*1024)} MB). "
                f"Download directly from {target} on the host.",
            )
        # Stream raw bytes — don't re-serialise; preserves whatever ordering
        # graphify wrote (callers may rely on `nodes`/`links` adjacency).
        return Response(
            content=target.read_bytes(),
            media_type="application/json",
        )

    @app.get("/graphify/{repo}/wiki/{wiki_path:path}")
    async def graphify_wiki(repo: str, wiki_path: str):
        # Empty path (`/graphify/{repo}/wiki/`) → default to index.md/.html.
        if not wiki_path or wiki_path.endswith("/"):
            wiki_path = (wiki_path or "") + "index.md"
        target = _resolve_graphify_path(repo, "wiki", *wiki_path.split("/"))
        # Allow falling back to .html if .md was requested but only .html exists
        # (or vice versa). Operators can switch generators without breaking links.
        if not target.is_file():
            if target.suffix == ".md":
                alt = target.with_suffix(".html")
                if alt.is_file():
                    target = alt
            elif target.suffix == ".html":
                alt = target.with_suffix(".md")
                if alt.is_file():
                    target = alt
        if not target.is_file():
            raise HTTPException(404, f"wiki file not found: {wiki_path}")
        # Markdown should render as text/plain so the browser shows raw MD,
        # letting the client choose to fetch+render. HTML renders inline.
        media_type, _ = mimetypes.guess_type(target.name)
        if target.suffix == ".md":
            media_type = "text/markdown; charset=utf-8"
        return FileResponse(target, media_type=media_type)

    # ── Mutations: add / remove / reindex ────────────────────────────

    # In-memory tracker for background reindex jobs. Dashboard polls
    # /api/jobs to surface progress without re-running the work.
    _jobs: dict[str, dict] = {}

    def _job_id() -> str:
        import uuid as _u
        return _u.uuid4().hex[:10]

    def _spawn_reindex(*, name: str, path: str, force: bool,
                       skip_summaries: bool, skip_chunks: bool) -> str:
        """Run flow.ingest_repo in a daemon thread; track in _jobs."""
        import threading
        import time as _t

        from aiforge_memory.core import state as state_db
        from aiforge_memory.features.flow import runner as flow

        jid = _job_id()
        _jobs[jid] = {
            "id": jid, "name": name, "path": path,
            "status": "running", "started_at": _t.time(),
            "force": force, "result": None, "error": None,
        }

        def _run() -> None:
            drv = _driver()
            sc = state_db.connect()
            try:
                res = flow.ingest_repo(
                    repo_name=name, repo_path=path,
                    driver=drv, state_conn=sc, force=force,
                    skip_summaries=skip_summaries,
                    skip_chunks=skip_chunks,
                )
                _jobs[jid].update({
                    "status": "ok",
                    "finished_at": _t.time(),
                    "result": {
                        "files":   getattr(res, "files", 0),
                        "symbols": getattr(res, "symbols", 0),
                        "chunks":  getattr(res, "chunks", 0),
                        "status":  getattr(res, "status", ""),
                    },
                })
            except Exception as exc:  # noqa: BLE001 — surfaced via API
                _jobs[jid].update({
                    "status": "error",
                    "finished_at": _t.time(),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
            finally:
                try:
                    drv.close()
                except Exception:
                    pass
                try:
                    sc.close()
                except Exception:
                    pass

        threading.Thread(target=_run, name=f"reindex-{name}",
                         daemon=True).start()
        return jid

    @app.post("/api/scheduler/add")
    async def scheduler_add(payload: dict):
        """Register a new repo with the scheduler. Required: name, path.
        Optional knobs: interval_seconds, pull, skip_summaries, skip_chunks,
        use_lsp, timeout_seconds, per_file_seconds, skip_services."""
        from pathlib import Path as _P

        from aiforge_memory.features.scheduler import runner as sched

        name = (payload.get("name") or "").strip()
        path = (payload.get("path") or "").strip()
        if not name or not path:
            raise HTTPException(400, "name + path required")
        rp = _P(path).expanduser()
        if not rp.is_dir():
            raise HTTPException(400, f"path not a directory: {path}")
        try:
            rs = sched.RepoSchedule(
                name=name, path=str(rp),
                interval_seconds=int(payload.get("interval_seconds", 600)),
                pull=bool(payload.get("pull", True)),
                skip_services=bool(payload.get("skip_services", False)),
                skip_summaries=bool(payload.get("skip_summaries", False)),
                skip_chunks=bool(payload.get("skip_chunks", False)),
                use_lsp=bool(payload.get("use_lsp", False)),
                timeout_seconds=int(payload.get("timeout_seconds", 1800)),
                per_file_seconds=float(payload.get("per_file_seconds", 0.0)),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"invalid field: {exc}") from None
        sched.add_repo(rs)
        return {"ok": True, "name": name,
                "hint": "scheduler reloads config each tick; "
                        "no daemon restart needed"}

    @app.delete("/api/scheduler/{name}")
    async def scheduler_remove(name: str):
        from aiforge_memory.features.scheduler import runner as sched
        removed = sched.remove_repo(name)
        if not removed:
            raise HTTPException(404, f"no scheduled repo named {name}")
        return {"ok": True, "name": name}

    @app.post("/api/repo/reindex")
    async def repo_reindex(payload: dict):
        """One-shot reindex (force=True by default). Runs in a worker
        thread; poll /api/jobs/{job_id} for progress.

        Path resolution order:
          1. payload['path']               explicit caller override
          2. scheduler.yaml entry          if registered
          3. Neo4j Repo node               from `path` / `local_path`
                                           field (set on first ingest)
        """
        from pathlib import Path as _P

        from aiforge_memory.features.scheduler import runner as sched

        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")

        path = (payload.get("path") or "").strip()
        path_source = "payload"

        if not path:
            cfg = sched.SchedulerConfig.load()
            for r in cfg.repos:
                if r.name == name:
                    path = r.path
                    path_source = "scheduler"
                    break

        if not path:
            # Fall back to Neo4j: many older repos have a Repo node but
            # were never registered with the scheduler.
            drv = _driver()
            try:
                with drv.session() as s:
                    rec = s.run(
                        "MATCH (r:Repo {name:$n}) "
                        "RETURN coalesce(r.path, r.local_path, "
                        "                r.repo_path, '') AS p",
                        n=name,
                    ).single()
                if rec and rec["p"]:
                    path = rec["p"]
                    path_source = "neo4j"
            finally:
                drv.close()

        if not path:
            raise HTTPException(
                400,
                f"no path known for {name}; pass `path` in body or "
                "register it via /api/scheduler/add",
            )
        if not _P(path).expanduser().is_dir():
            raise HTTPException(
                400,
                f"path resolved to {path} but is not a directory",
            )

        jid = _spawn_reindex(
            name=name, path=path,
            force=bool(payload.get("force", True)),
            skip_summaries=bool(payload.get("skip_summaries", False)),
            skip_chunks=bool(payload.get("skip_chunks", False)),
        )
        return {"job_id": jid, "name": name, "path": path,
                "path_source": path_source}

    @app.get("/api/jobs")
    async def jobs_list():
        # Newest first.
        return sorted(_jobs.values(),
                      key=lambda j: j.get("started_at", 0), reverse=True)

    @app.get("/api/jobs/{job_id}")
    async def jobs_get(job_id: str):
        j = _jobs.get(job_id)
        if not j:
            raise HTTPException(404, f"unknown job {job_id}")
        return j

    @app.delete("/api/repo/{name}")
    async def repo_delete(name: str, purge: bool = True,
                          drop_schedule: bool = True):
        """Destructive: remove the repo's Neo4j nodes (File_v2 / Symbol_v2 /
        Chunk_v2 / memory nodes / Repo) and optionally unregister from the
        scheduler. Returns a per-label count of deleted nodes.

        Query params:
          purge=true (default)         drop graph nodes
          drop_schedule=true (default) remove from scheduler.yaml
        """
        from aiforge_memory.features.scheduler import runner as sched

        deleted = {"files": 0, "symbols": 0, "chunks": 0,
                   "memory": 0, "repo": 0}
        if purge:
            drv = _driver()
            try:
                with drv.session() as s:
                    # Single transaction so a failure mid-cascade rolls
                    # back rather than leaving orphan nodes.
                    rec = s.run("""
                        MATCH (r:Repo {name:$n})
                        OPTIONAL MATCH (r)-[:CONTAINS_FILE]->(f:File_v2)
                        OPTIONAL MATCH (f)-[:DEFINES]->(sym:Symbol_v2)
                        OPTIONAL MATCH (f)-[:CHUNKED_AS]->(c:Chunk_v2)
                        OPTIONAL MATCH (r)-[:RECORDS]->(mem)
                        WHERE any(l IN labels(mem)
                                  WHERE l ENDS WITH '_v2')
                        WITH r,
                             collect(DISTINCT f)   AS fs,
                             collect(DISTINCT sym) AS ss,
                             collect(DISTINCT c)   AS cs,
                             collect(DISTINCT mem) AS ms
                        WITH r, fs, ss, cs, ms,
                             size(fs) AS nf, size(ss) AS nsym,
                             size(cs) AS nc, size(ms) AS nm
                        FOREACH (x IN ss | DETACH DELETE x)
                        FOREACH (x IN cs | DETACH DELETE x)
                        FOREACH (x IN fs | DETACH DELETE x)
                        FOREACH (x IN ms | DETACH DELETE x)
                        DETACH DELETE r
                        RETURN nf AS files, nsym AS symbols,
                               nc AS chunks, nm AS memory
                    """, n=name).single()
                    if rec:
                        deleted.update({
                            "files":   rec["files"],
                            "symbols": rec["symbols"],
                            "chunks":  rec["chunks"],
                            "memory":  rec["memory"],
                            "repo":    1,
                        })
                    else:
                        deleted["repo"] = 0
            finally:
                drv.close()
        # Remove from scheduler config — best-effort, non-fatal.
        scheduler_removed = False
        if drop_schedule:
            scheduler_removed = bool(sched.remove_repo(name))
        return {"name": name, "deleted": deleted,
                "scheduler_removed": scheduler_removed}

    return app


def serve(host: str = "127.0.0.1", port: int = 8767) -> None:
    """Run the UI server. Blocking."""
    if not _HAS_FASTAPI:
        raise RuntimeError(
            "fastapi/uvicorn not installed. Run: uv pip install '.[ui]'"
        )
    import uvicorn
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
