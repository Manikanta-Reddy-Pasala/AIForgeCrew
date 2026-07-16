"""Loop — foreground scheduler sweep with Neo4j backoff and decay."""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from ._config import SchedulerConfig
from ._defaults import _default_driver, _default_state, _log_to
from ._status import _read_status, _write_status
from ._tick import tick_repo


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
