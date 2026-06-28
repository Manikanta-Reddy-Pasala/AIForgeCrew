"""Scheduler daemon — periodic git fetch + delta ingest per repo.

Reads `~/.aiforge/scheduler.yaml`:

    repos:
      - name: PosClientBackend
        path: /Users/me/code/pcb
        interval_seconds: 600          # default 600
        pull: true                      # ff-only pull from origin
        skip_summaries: false
        skip_chunks: false
      - name: PosServerBackend
        path: /Users/me/code/psb
        interval_seconds: 1800

Run modes:
    aiforge-memory schedule run        # foreground; Ctrl-C to stop
    aiforge-memory schedule daemon     # background fork; pidfile in
                                       # ~/.aiforge/scheduler.pid
    aiforge-memory schedule status     # JSON: per-repo last_run, next_run
    aiforge-memory schedule add        # mutate yaml
    aiforge-memory schedule remove
    aiforge-memory schedule list

Safety:
    - `git pull --ff-only` only — refuses on divergence (no rebase, no merge).
    - Per-repo lockfile prevents overlapping runs.
    - SIGINT / SIGTERM handled cleanly; in-flight delta finishes.
    - If repo's working tree is dirty (tracked-file mods), the pull is
      skipped and a warning logged; ingest still runs to capture local
      uncommitted state via merkle fallback.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_CONFIG",
        os.path.expanduser("~/.aiforge/scheduler.yaml"),
    )
)
PID_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_PIDFILE",
        os.path.expanduser("~/.aiforge/scheduler.pid"),
    )
)
STATUS_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_STATUS",
        os.path.expanduser("~/.aiforge/scheduler.status.json"),
    )
)
LOG_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_LOG",
        os.path.expanduser("~/.aiforge/scheduler.log"),
    )
)


# ─── Config ───────────────────────────────────────────────────────────

@dataclass
class RepoSchedule:
    name: str
    path: str
    interval_seconds: int = 600
    pull: bool = True
    skip_services: bool = False    # skip Stage 3 (LLM service catalog)
    skip_summaries: bool = False
    skip_chunks: bool = False
    use_lsp: bool = False          # opt-in LSP-confirmed CALLS
    timeout_seconds: int = 1800    # FLOOR for the per-tick wall ceiling.
                                   # Effective timeout grows with file
                                   # count when per_file_seconds > 0.
    per_file_seconds: float = 0.0  # 0 = fixed timeout. >0 enables
                                   # dynamic scaling: timeout =
                                   # max(timeout_seconds,
                                   #     file_count × per_file_seconds),
                                   # capped by AIFORGE_SCHEDULER_MAX_TIMEOUT_S
                                   # (default 14400s / 4 hr).


@dataclass
class SchedulerConfig:
    repos: list[RepoSchedule] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> SchedulerConfig:
        path = Path(path or CONFIG_PATH)
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return cls()
        repos: list[RepoSchedule] = []
        for r in data.get("repos") or []:
            try:
                repos.append(RepoSchedule(
                    name=str(r["name"]),
                    path=str(r["path"]),
                    interval_seconds=int(r.get("interval_seconds", 600)),
                    pull=bool(r.get("pull", True)),
                    skip_services=bool(r.get("skip_services", False)),
                    skip_summaries=bool(r.get("skip_summaries", False)),
                    skip_chunks=bool(r.get("skip_chunks", False)),
                    use_lsp=bool(r.get("use_lsp", False)),
                    timeout_seconds=int(r.get("timeout_seconds", 1800)),
                    per_file_seconds=float(r.get("per_file_seconds", 0.0)),
                ))
            except (KeyError, ValueError):
                continue
        return cls(repos=repos)

    def save(self, path: Path | None = None) -> None:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            {"repos": [asdict(r) for r in self.repos]},
            default_flow_style=False, sort_keys=False,
        ))


def add_repo(rs: RepoSchedule, *, path: Path | None = None) -> None:
    cfg = SchedulerConfig.load(path)
    cfg.repos = [r for r in cfg.repos if r.name != rs.name]
    cfg.repos.append(rs)
    cfg.save(path)


def remove_repo(name: str, *, path: Path | None = None) -> bool:
    cfg = SchedulerConfig.load(path)
    before = len(cfg.repos)
    cfg.repos = [r for r in cfg.repos if r.name != name]
    cfg.save(path)
    return len(cfg.repos) < before


# ─── Git helpers (poll-decide) ────────────────────────────────────────

@dataclass
class FetchOutcome:
    fetched: bool                  # `git fetch` succeeded
    pulled: bool                   # `git pull --ff-only` succeeded
    behind: int                    # commits behind upstream BEFORE pull
    skipped_reason: str = ""       # 'dirty' | 'no_upstream' | ''


def fetch_and_maybe_pull(
    repo_path: str | Path, *, do_pull: bool = True,
) -> FetchOutcome:
    """git fetch; report ahead/behind; ff-only pull if behind & clean.

    - Refuses to pull when working tree is dirty.
    - Refuses to pull when upstream is not configured.
    - ff-only — won't merge or rebase divergent histories.
    """
    cwd = str(Path(repo_path).resolve())

    fetch_ok = _git_run(cwd, "fetch", "--quiet").returncode == 0
    if not fetch_ok:
        return FetchOutcome(False, False, 0)

    behind = _commits_behind(cwd)
    if behind <= 0:
        return FetchOutcome(True, False, 0)

    if not do_pull:
        return FetchOutcome(True, False, behind)

    if _is_dirty(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="dirty")

    if not _has_upstream(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="no_upstream")

    pull = _git_run(cwd, "pull", "--ff-only", "--quiet")
    return FetchOutcome(True, pull.returncode == 0, behind)


def _git_run(cwd: str, *args: str, timeout: int = 30):
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _commits_behind(cwd: str) -> int:
    """Count of commits HEAD is behind @{u}. 0 if up-to-date or unknown."""
    r = _git_run(cwd, "rev-list", "--count", "HEAD..@{u}")
    if r.returncode != 0:
        return 0
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return 0


def _is_dirty(cwd: str) -> bool:
    r = _git_run(cwd, "status", "--porcelain=v1", "-uno")
    return bool((r.stdout or "").strip())


def _has_upstream(cwd: str) -> bool:
    return _git_run(
        cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
    ).returncode == 0


# ─── Per-repo lockfile ────────────────────────────────────────────────

def _lockfile(name: str) -> Path:
    return Path(os.path.expanduser(f"~/.aiforge/lock.{_safe(name)}.pid"))


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def _acquire_lock(name: str) -> bool:
    lf = _lockfile(name)
    if lf.exists():
        try:
            pid = int(lf.read_text().strip())
            os.kill(pid, 0)              # signal 0 = check if alive
            return False                 # still alive — locked
        except (ValueError, OSError, ProcessLookupError):
            pass                         # stale; reclaim
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))
    return True


def _release_lock(name: str) -> None:
    try:
        _lockfile(name).unlink()
    except FileNotFoundError:
        pass


# ─── Status journal ───────────────────────────────────────────────────

@dataclass
class RepoStatus:
    name: str
    last_run: float = 0.0          # unix ts
    last_status: str = ""          # 'delta_applied'|'no_changes'|'error'|...
    last_pulled: bool = False
    last_behind: int = 0
    last_error: str = ""
    next_run: float = 0.0


def _read_status() -> dict[str, RepoStatus]:
    if not STATUS_PATH.is_file():
        return {}
    try:
        raw = json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, RepoStatus] = {}
    for name, d in (raw or {}).items():
        out[name] = RepoStatus(name=name, **{
            k: d.get(k, getattr(RepoStatus(name=name), k))
            for k in ("last_run", "last_status", "last_pulled",
                      "last_behind", "last_error", "next_run")
        })
    return out


def _write_status(d: dict[str, RepoStatus]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(
        {n: asdict(s) for n, s in d.items()}, indent=2,
    ))


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


# ─── Loop ─────────────────────────────────────────────────────────────

class _StopFlag:
    """Cooperative shutdown flag toggled by SIGINT/SIGTERM."""
    def __init__(self) -> None:
        self.stop = False

    def trip(self, *_args) -> None:
        self.stop = True


def run_loop(
    *,
    config: SchedulerConfig | None = None,
    driver_factory=None,
    state_factory=None,
    log_path: Path | None = None,
    once: bool = False,
) -> None:
    """Foreground scheduler loop. ``once=True`` runs each repo a single
    time and exits — useful for cron + tests."""
    cfg = config or SchedulerConfig.load()
    if not cfg.repos:
        _log_to(log_path, "no repos configured in scheduler.yaml; exiting")
        return

    driver = (driver_factory or _default_driver)()
    state_conn = (state_factory or _default_state)()

    flag = _StopFlag()
    signal.signal(signal.SIGINT, flag.trip)
    signal.signal(signal.SIGTERM, flag.trip)

    # Per-repo "next-due" timestamps, all initially due now.
    due: dict[str, float] = {r.name: 0.0 for r in cfg.repos}
    statuses = _read_status()

    def log(msg: str) -> None:
        _log_to(log_path, msg)

    # Exponential-backoff wait when Neo4j keeps failing across the loop.
    # Caps at 5 minutes so we keep retrying but don't spam logs / drivers.
    backoff_seconds = 0
    BACKOFF_MAX = 300

    # Memory decay — once per AIFORGE_DECAY_INTERVAL_S (default daily),
    # checked at most once per sweep. First sweep runs it immediately.
    decay_interval = int(os.environ.get("AIFORGE_DECAY_INTERVAL_S", "86400"))
    decay_age_days = int(os.environ.get("AIFORGE_DECAY_AGE_DAYS", "30"))
    next_decay_at = 0.0

    while not flag.stop:
        now = time.time()
        any_neo4j_down = False
        for rs in cfg.repos:
            if flag.stop:
                break
            if now < due.get(rs.name, 0):
                continue
            st = tick_repo(
                rs, driver=driver, state_conn=state_conn, log=log,
            )
            statuses[rs.name] = st
            due[rs.name] = st.next_run
            _write_status(statuses)
            if st.last_status == "neo4j_down":
                any_neo4j_down = True

        if not flag.stop and time.time() >= next_decay_at:
            try:
                from aiforge_memory.features.memory import decay
                res = decay.run_decay(driver, max_age_days=decay_age_days)
                log(f"decay: archived={res['archived']} "
                    f"max_age_days={res['max_age_days']}")
            except Exception as exc:  # noqa: BLE001 — decay is best-effort
                log(f"decay failed: {exc!r}")
            next_decay_at = time.time() + decay_interval

        if any_neo4j_down:
            backoff_seconds = min(BACKOFF_MAX,
                                  max(15, backoff_seconds * 2 or 15))
            log(f"neo4j_down detected; backing off {backoff_seconds}s "
                "before next sweep")
            # Try to refresh the driver too — connection may be stale.
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                driver = (driver_factory or _default_driver)()
            except Exception as exc:  # noqa: BLE001
                log(f"driver re-open failed: {exc!r}")
        else:
            backoff_seconds = 0

        if once:
            break
        # Sleep until the soonest due, capped at 5s for responsiveness
        # — unless we're in Neo4j backoff, then sleep the backoff window.
        if backoff_seconds:
            end = time.time() + backoff_seconds
        else:
            wait = max(1.0, min(due.values(), default=now + 60) - time.time())
            wait = min(wait, 5.0)
            end = time.time() + wait
        # Cooperative sleep that wakes on signal.
        while not flag.stop and time.time() < end:
            time.sleep(0.5)

    _log_to(log_path, "scheduler shutting down")
    try:
        driver.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Daemonize (POSIX double-fork) ────────────────────────────────────

def daemonize(*, log_path: Path | None = None) -> int:
    """POSIX double-fork. Parent returns child PID. Child runs run_loop."""
    if PID_PATH.is_file():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            raise RuntimeError(f"scheduler already running, pid={pid}")
        except (ValueError, ProcessLookupError):
            pass

    pid = os.fork()
    if pid > 0:
        # First parent — wait for first child to fork its own and exit.
        os.waitpid(pid, 0)
        # Read pid the grandchild wrote.
        for _ in range(50):
            if PID_PATH.exists():
                try:
                    return int(PID_PATH.read_text().strip())
                except ValueError:
                    pass
            time.sleep(0.05)
        return -1

    # First child
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — actual daemon
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    os.chdir("/")
    # Detach stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    try:
        run_loop(log_path=log_path)
    finally:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        os._exit(0)


def stop_daemon(*, wait_seconds: float = 30.0) -> bool:
    """Send SIGTERM and wait up to wait_seconds for actual exit.

    The previous implementation returned True the moment the signal was
    sent, leaving callers (like a redeploy script) to race with the PID
    file. Now we poll until the process is gone, then unlink the PID
    file so a subsequent ``daemonize()`` does not raise
    ``scheduler already running``.
    """
    if not PID_PATH.is_file():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)        # signal 0 = liveness probe
            time.sleep(0.5)
        except ProcessLookupError:
            break

    # SIGKILL fallback if it ignored SIGTERM.
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    except ProcessLookupError:
        pass

    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return True


def daemon_status() -> dict:
    out: dict = {"pid_file": str(PID_PATH), "running": False, "pid": None}
    if PID_PATH.is_file():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            out["running"] = True
            out["pid"] = pid
        except (ValueError, ProcessLookupError):
            pass
    out["repos"] = {n: asdict(s) for n, s in _read_status().items()}
    return out


# ─── Defaults ─────────────────────────────────────────────────────────

def _default_driver():
    from aiforge_memory.core.neo4j import open_driver
    return open_driver()


def _default_state():
    from aiforge_memory.core import state as sdb
    conn = sdb.open_db()
    sdb.migrate(conn)
    return conn


def _log_to(path: Path | None, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    target = Path(path or LOG_PATH)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(line)
    except OSError:
        pass
