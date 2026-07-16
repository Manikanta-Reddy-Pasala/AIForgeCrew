"""Daemonize — POSIX double-fork daemon control (start/stop/status)."""
from __future__ import annotations

import os
import signal
import time
from dataclasses import asdict
from pathlib import Path

from ._loop import run_loop
from ._paths import PID_PATH
from ._status import _read_status


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
