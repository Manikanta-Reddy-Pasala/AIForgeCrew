"""No command outlives the server that started it.

A handed-off command runs in its own process group, and a turn's jobs are
killed when the turn ends. When the server itself stops first (a restart, a
crash, an operator kill), nothing killed them: a probe that looped forever
kept one CPU at 100% for four days after its run.

* At exit, every live handed-off job (not an explicit background job or a
  service — those are meant to keep running) is killed.
* Each such job's process group is recorded in ``<config>/run/jobs.json``;
  a new server sweeps the groups left by a server that is gone, when the
  group leader still runs the recorded command (no pid-reuse kills; Linux
  /proc only).

AIFORGE_JOB_REAPER=0 turns both off.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import threading

_LOCK = threading.Lock()
_SWEPT = False


def _on() -> bool:
    return os.environ.get("AIFORGE_JOB_REAPER", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _path() -> str:
    try:
        from aiforge_core.config.paths import config_dir
        base = str(config_dir())
    except Exception:  # noqa: BLE001
        base = os.path.expanduser("~/.aiforge")
    return os.path.join(base, "run", "jobs.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def _cmdline(pid: int) -> "str | None":
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _tracked(job) -> bool:
    return bool(getattr(job, "pgid", None)) and not getattr(job, "explicit", False)


def record(job) -> None:
    if not _on() or not _tracked(job):
        return
    with _LOCK:
        data = _load()
        data[str(job.pgid)] = {"server": os.getpid(),
                               "cmd": str(getattr(job, "cmd", "") or "")[:200]}
        _save(data)


def forget(job) -> None:
    if not _on() or not _tracked(job):
        return
    with _LOCK:
        data = _load()
        if data.pop(str(job.pgid), None) is not None:
            _save(data)


def _matches(pgid: int, cmd: str) -> bool:
    """The group leader still runs (part of) the recorded command."""
    line = _cmdline(pgid)
    if line is None:
        return False
    words = [w for w in cmd.replace('"', " ").replace("'", " ").split() if len(w) > 3][:4]
    return bool(words) and all(w in line for w in words[:2])


def sweep() -> int:
    """Kill job groups a dead server left behind. Once per process."""
    global _SWEPT
    if _SWEPT or not _on():
        return 0
    _SWEPT = True
    killed = 0
    with _LOCK:
        data = _load()
        keep = {}
        for key, row in data.items():
            try:
                pgid, server = int(key), int(row.get("server") or 0)
            except (TypeError, ValueError):
                continue
            if server and server != os.getpid() and _alive(server):
                keep[key] = row               # its server still runs it
                continue
            if _matches(pgid, str(row.get("cmd") or "")):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
        _save(keep)
    return killed


def kill_on_exit() -> int:
    if not _on():
        return 0
    killed = 0
    try:
        from aiforge_core.runtime import cmd_jobs
        with cmd_jobs._LOCK:
            jobs = list(cmd_jobs._JOBS.values())
        for job in jobs:
            if _tracked(job) and job.alive():
                try:
                    os.killpg(int(job.pgid), signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
                forget(job)
    except Exception:  # noqa: BLE001 — exit must never fail
        pass
    return killed


atexit.register(kill_on_exit)
