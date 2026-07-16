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

This module was split (grouped by concern) into ``_paths`` / ``_config`` /
``_git`` / ``_lock`` / ``_status`` / ``_tick`` / ``_loop`` / ``_daemon`` /
``_defaults`` submodules; this package re-exports the full former top-level
surface so ``from aiforge_memory.features.scheduler import runner`` and every
``runner.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._config import (
    RepoSchedule,
    SchedulerConfig,
    add_repo,
    remove_repo,
)
from ._daemon import (
    daemon_status,
    daemonize,
    stop_daemon,
)
from ._defaults import (
    _default_driver,
    _default_state,
    _log_to,
)
from ._git import (
    FetchOutcome,
    _commits_behind,
    _git_run,
    _has_upstream,
    _is_dirty,
    fetch_and_maybe_pull,
)
from ._lock import (
    _acquire_lock,
    _lockfile,
    _release_lock,
    _safe,
)
from ._loop import (
    _StopFlag,
    run_loop,
)
from ._paths import (
    CONFIG_PATH,
    LOG_PATH,
    PID_PATH,
    STATUS_PATH,
)
from ._status import (
    RepoStatus,
    _read_status,
    _write_status,
)
from ._tick import (
    _INGEST_EXT,
    _LIVE_WORKERS,
    _count_ingest_files,
    _effective_timeout,
    tick_repo,
)

__all__ = [
    "CONFIG_PATH", "PID_PATH", "STATUS_PATH", "LOG_PATH",
    "RepoSchedule", "SchedulerConfig", "add_repo", "remove_repo",
    "FetchOutcome", "fetch_and_maybe_pull",
    "RepoStatus",
    "tick_repo",
    "run_loop",
    "daemonize", "stop_daemon", "daemon_status",
]
