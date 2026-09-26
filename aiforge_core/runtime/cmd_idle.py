"""Is a running shell command still doing anything?

A fixed wall clock (600s) killed healthy work: a full maven build, a large
``npm ci``, a long test suite. What actually marks a HUNG command is silence —
no output AND no CPU for a long while (a prompt waiting on stdin, a network
read that will never return, a deadlock). A command that keeps printing, or
keeps burning CPU while it compiles quietly, is working and runs unbounded.

  AIFORGE_CMD_IDLE_S — seconds of no output and no CPU progress before the
                       command is treated as hung (default 600; 0 = never).

CPU is read for the command's whole process group (the shell's children do
the work). psutil when present, else ``/proc`` on Linux; where neither can
tell, output alone decides.
"""
from __future__ import annotations

import os
import time


def idle_limit_s(env: str = "AIFORGE_CMD_IDLE_S", default: float = 600.0) -> float:
    try:
        return max(0.0, float(os.environ.get(env, default)))
    except (TypeError, ValueError):
        return default


def wall_cap_s(explicit, env: str) -> float:
    """A hard wall clock, when one was ASKED for: the model's per-call
    ``timeout`` argument, else the operator's legacy knob ``env`` when set.
    0 means none — the idle detector alone decides."""
    raw = explicit if explicit not in (None, "") else os.environ.get(env, "")
    try:
        return max(0.0, float(raw)) if str(raw).strip() else 0.0
    except (TypeError, ValueError):
        return 0.0


def _proc_cpu(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        tick = os.sysconf("SC_CLK_TCK")
        # utime, stime, cutime, cstime (fields 14-17; 12-15 after the comm)
        return sum(int(f) for f in fields[11:15]) / float(tick)
    except (OSError, IndexError, ValueError):
        return None


def group_cpu_s(pgid: int | None) -> float | None:
    """Total CPU seconds used by every process in group ``pgid``, or None when
    this platform cannot say."""
    if pgid is None:
        return None
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        total, seen = 0.0, False
        for p in psutil.process_iter(["pid"]):
            try:
                if os.getpgid(p.info["pid"]) != pgid:
                    continue
                t = p.cpu_times()
                total += t.user + t.system + getattr(t, "children_user", 0.0) \
                    + getattr(t, "children_system", 0.0)
                seen = True
            except Exception:  # noqa: BLE001 — gone / no access
                continue
        return total if seen else None
    if not os.path.isdir("/proc"):
        return None
    from aiforge_core.runtime.chat_agent._spool import _group_members
    vals = [v for v in (_proc_cpu(pid) for pid in _group_members(pgid))
            if v is not None]
    return sum(vals) if vals else None


class ProgressClock:
    """Tracks the last moment a command showed life.

    ``output_size`` is a zero-arg callable returning bytes written so far.
    Output is checked on every :meth:`stalled` call (cheap); CPU only once the
    output has been quiet for the whole idle window, so a chatty build never
    pays for a process-table walk."""

    def __init__(self, pgid: int | None, output_size, idle_s: float,
                 clock=time.monotonic) -> None:
        self.pgid = pgid
        self.output_size = output_size
        self.idle_s = idle_s
        self.clock = clock
        self.last = clock()
        self._size = self._safe_size()
        # No CPU sample up front (a process-table walk on every command start
        # buys nothing): the group was born with the clock, so its first
        # sample IS the CPU spent since start.
        self._cpu: float | None = None
        self._cpu_at = float("-inf")

    def _safe_size(self) -> int:
        try:
            return int(self.output_size())
        except Exception:  # noqa: BLE001
            return 0

    def _cpu_moved(self) -> bool:
        cpu = group_cpu_s(self.pgid)
        if cpu is None:
            return False
        # A process that is merely asleep still accrues a few ticks; ask for
        # a real fraction of the window before calling it progress.
        moved = cpu - (self._cpu or 0.0) >= min(1.0, 0.02 * self.idle_s)
        self._cpu = cpu
        return moved

    def stalled(self) -> bool:
        if not self.idle_s:
            return False
        now = self.clock()
        size = self._safe_size()
        if size != self._size:
            self._size, self.last = size, now
            return False
        if now - self.last < self.idle_s:
            return False
        # Past the window: CPU is sampled at most every few seconds (a
        # caller polling 4x a second must not walk the process table 4x).
        if now - self._cpu_at >= min(5.0, self.idle_s / 4):
            self._cpu_at = now
            if self._cpu_moved():
                self.last = now
                return False
        return True


__all__ = ["ProgressClock", "group_cpu_s", "idle_limit_s", "wall_cap_s"]
