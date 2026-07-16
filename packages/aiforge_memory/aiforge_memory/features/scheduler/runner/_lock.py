"""Per-repo lockfile — prevents overlapping runs for one repo."""
from __future__ import annotations

import os
from pathlib import Path


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
