"""Scheduler filesystem paths (config / pidfile / status / log)."""
from __future__ import annotations

import os
from pathlib import Path

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
