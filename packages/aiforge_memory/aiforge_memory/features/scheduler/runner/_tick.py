"""Tick — one fetch + delta ingest cycle per repo, with timeout scaling."""
from __future__ import annotations

import time

from ._config import RepoSchedule
from ._git import fetch_and_maybe_pull
from ._lock import _acquire_lock, _release_lock
from ._status import RepoStatus


# ─── Tick ──────────────────────────────────────────────────────────────

# File extensions that actually trigger LLM/embed work — skip vendored
# bundles, lockfiles, and binary artifacts so the scaling estimate
# matches reality. Mirrors the ingest walker's allowlist (kept loose;
# under-counting is safer than over-counting because the floor catches
# small repos anyway).
_INGEST_EXT = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".scala",
    ".go", ".rs", ".rb", ".php", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".m", ".mm",
    ".md", ".rst", ".adoc", ".txt",
    ".yml", ".yaml", ".json", ".toml",
)


def _count_ingest_files(repo_path: str) -> int:
    """Fast best-effort file count under repo_path with ingest-relevant
    extensions. Skips ``.git``, ``node_modules``, ``.venv``, ``target``,
    ``build``, ``dist`` so vendored trees don't inflate the estimate.

    Used for dynamic timeout scaling — exactness doesn't matter, ±20%
    is fine because the floor (rs.timeout_seconds) catches under-counts."""
    import os as _os
    if not repo_path:
        return 0
    skip_dirs = {
        ".git", "node_modules", ".venv", "venv", "target", "build",
        "dist", ".idea", ".vscode", ".aiforge", ".aiforge-worktrees",
        "graphify-out", "__pycache__", "coverage",
    }
    total = 0
    for root, dirs, files in _os.walk(repo_path):
        # In-place prune so os.walk doesn't descend into vendored trees.
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith(_INGEST_EXT):
                total += 1
    return total


def _effective_timeout(rs: RepoSchedule, repo_path: str) -> tuple[int, int]:
    """Resolve the wall-clock timeout for one tick, returning
    ``(timeout_s, file_count)``.

    Behaviour:
      - If ``rs.per_file_seconds <= 0`` → ``rs.timeout_seconds`` (legacy).
      - Else → ``max(rs.timeout_seconds, file_count × per_file_seconds)``,
        capped by ``AIFORGE_SCHEDULER_MAX_TIMEOUT_S`` (default 14400s).
    """
    import os as _os
    if rs.per_file_seconds <= 0:
        return rs.timeout_seconds, 0
    file_count = _count_ingest_files(repo_path)
    scaled = int(file_count * rs.per_file_seconds)
    try:
        cap = int(_os.environ.get("AIFORGE_SCHEDULER_MAX_TIMEOUT_S",
                                  "14400"))
    except ValueError:
        cap = 14400
    return min(cap, max(rs.timeout_seconds, scaled)), file_count


# Live (timed-out but still running) worker threads keyed by repo name.
# A timed-out tick leaves its worker here with the lock still held;
# subsequent ticks skip with 'still_running' until the thread is
# observed dead, at which point the lock is released. Prevents the old
# double-ingest where the timeout branch freed the lock under a zombie.
_LIVE_WORKERS: dict[str, object] = {}


def tick_repo(
    rs: RepoSchedule, *, driver, state_conn, log,
) -> RepoStatus:
    """Run one fetch + delta cycle for a repo. Returns RepoStatus update.

    Resilience:
      - Per-tick wall timeout (rs.timeout_seconds) — runs the ingest in
        a worker thread, joins with timeout. Long-running stages can't
        block the loop forever.
      - A timed-out worker keeps the per-repo lock until it is observed
        dead (see _LIVE_WORKERS) so a zombie ingest can't overlap the
        next tick's ingest.
      - Neo4j connection errors set status='neo4j_down' so a watchdog
        can react. Caller may apply exponential backoff.
      - LSP opt-in via rs.use_lsp.
    """
    import threading

    from aiforge_memory.features.delta import extract as delta
    from aiforge_memory.features.flow import runner as flow

    status = RepoStatus(name=rs.name)
    status.last_run = time.time()
    status.next_run = status.last_run + rs.interval_seconds

    prior = _LIVE_WORKERS.get(rs.name)
    if prior is not None:
        if prior.is_alive():
            status.last_status = "still_running"
            log(f"[{rs.name}] timed-out worker still running; skipped")
            return status
        # Zombie finished since the timeout — free its lock and proceed.
        _LIVE_WORKERS.pop(rs.name, None)
        _release_lock(rs.name)

    if not _acquire_lock(rs.name):
        status.last_status = "locked"
        log(f"[{rs.name}] previous run still active; skipped")
        return status

    result_box: dict = {"res": None, "exc": None, "out": None}

    def _work() -> None:
        try:
            out = fetch_and_maybe_pull(rs.path, do_pull=rs.pull)
            result_box["out"] = out
            res = delta.ingest_delta(
                repo_name=rs.name, repo_path=rs.path,
                driver=driver, state_conn=state_conn,
                skip_summaries=rs.skip_summaries,
                skip_chunks=rs.skip_chunks,
                use_lsp=rs.use_lsp,
            )
            if res.status == "cold_start_required":
                log(f"[{rs.name}] cold_start_required → running full ingest")
                res = flow.ingest_repo(
                    repo_name=rs.name, repo_path=rs.path,
                    driver=driver, state_conn=state_conn, force=False,
                    skip_services=rs.skip_services,
                    skip_summaries=rs.skip_summaries,
                    skip_chunks=rs.skip_chunks,
                    use_lsp=rs.use_lsp,
                )
            result_box["res"] = res
        except Exception as exc:  # noqa: BLE001 — must surface to outer
            result_box["exc"] = exc

    eff_timeout, file_count = _effective_timeout(rs, rs.path)
    worker = None
    try:
        worker = threading.Thread(target=_work, name=f"tick-{rs.name}",
                                  daemon=True)
        worker.start()
        worker.join(timeout=eff_timeout)
        if worker.is_alive():
            status.last_status = "timeout"
            scale = (f", scaled from files={file_count} × {rs.per_file_seconds}s/file"
                     if rs.per_file_seconds > 0 else "")
            status.last_error = f"tick exceeded {eff_timeout}s{scale}"
            log(f"[{rs.name}] timeout after {eff_timeout}s{scale} — "
                "worker kept; lock held until it is observed dead")
            # Worker thread keeps running (daemon=True so it dies on
            # process exit). The finally block registers it and keeps
            # the lock so the next tick can't double-ingest.
            return status

        if result_box["exc"] is not None:
            exc = result_box["exc"]
            err_text = str(exc)
            # Classify Neo4j-down errors so watchdogs can react.
            if any(s in err_text.lower() for s in (
                "service unavailable", "session expired",
                "connection refused", "unable to retrieve routing",
            )):
                status.last_status = "neo4j_down"
            else:
                status.last_status = "error"
            status.last_error = err_text[:240]
            log(f"[{rs.name}] {status.last_status}: {exc!r}")
            return status

        out = result_box["out"]
        res = result_box["res"]
        status.last_pulled = out.pulled if out else False
        status.last_behind = out.behind if out else 0
        if out and out.skipped_reason:
            log(f"[{rs.name}] pull skipped: {out.skipped_reason}; "
                f"behind={out.behind}")
        status.last_status = res.status
        log(f"[{rs.name}] {res.status} files={res.files_count} "
            f"symbols={res.symbols_count} chunks={res.chunks_count} "
            f"pulled={status.last_pulled} behind={status.last_behind} "
            f"lsp={rs.use_lsp}")
    finally:
        if worker is not None and worker.is_alive():
            # Timed out — keep the lock; the registry entry lets a later
            # tick release it once the worker is observed dead.
            _LIVE_WORKERS[rs.name] = worker
        else:
            _LIVE_WORKERS.pop(rs.name, None)
            _release_lock(rs.name)

    return status
