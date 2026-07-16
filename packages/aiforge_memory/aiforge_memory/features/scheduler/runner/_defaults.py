"""Defaults — driver/state factories and the file logger."""
from __future__ import annotations

import time
from pathlib import Path

from ._paths import LOG_PATH


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
