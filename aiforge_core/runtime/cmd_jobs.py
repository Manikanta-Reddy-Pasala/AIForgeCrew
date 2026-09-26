"""Running commands the agent CHECKS ON instead of blindly waiting for.

A foreground ``run_command`` that is still running after a short check-in
(``AIFORGE_CMD_CHECKIN_S``, default 15s) is not killed and not waited out: it
becomes a job, and the agent gets back what it printed so far, whether the
output is still growing and whether it is using CPU. It then decides —
``command_wait`` for more, ``command_output`` to peek, ``command_kill`` to stop
it and fix the command. Background commands (``background: true`` / a
trailing ``&``) and ``serve`` services are jobs too, so the same three tools
work on them.

A wait ends EARLY — before its time is up — on anything that should change
what the agent does next: the command exited, its new output shows a failure
or a prompt waiting for input (:mod:`aiforge_core.runtime.cmd_signals`), or it
looks stuck (no output and no CPU for ``AIFORGE_CMD_STUCK_CHECK_S``, default
45s; reported, not killed — the output-idle kill stays the last-resort guard).
A healthy wait that simply ran its time doubles the next default wait
(15→30→60→120, cap 300); any signal resets it.

Jobs a turn handed off are killed when that turn ends (an explicit background
command is left running, as before). Stop kills them all through bg_work.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

from aiforge_core.runtime import cmd_signals as sig

_TURN: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_cmd_turn", default=None)
#: Who a job belongs to beyond the turn: a pipeline ticket (``ticket-12``),
#: so a claim that ends — or is taken over after a crash — can stop them.
_OWNER: contextvars.ContextVar = contextvars.ContextVar(
    "aiforge_cmd_owner", default=None)
_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
_MAX_JOBS = 64
TAIL_CHARS = 3000
_POLL_S = 0.25


def _env_s(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def checkin_s() -> float:
    return _env_s("AIFORGE_CMD_CHECKIN_S", 15.0)


def stuck_s() -> float:
    return _env_s("AIFORGE_CMD_STUCK_CHECK_S", 45.0)


class Job:
    """One running (or finished, not yet collected) command."""

    def __init__(self, key: str, proc, cmd: str, streams: list, *,
                 session_id=None, explicit: bool = False, kill=None,
                 close=None, pgid=None) -> None:
        from aiforge_core.runtime.cmd_idle import ProgressClock
        self.key, self.proc, self.cmd = key, proc, cmd
        self.streams = streams            # file objects or paths
        self.seen = [0] * len(streams)
        self.session_id, self.explicit = session_id, explicit
        self._kill, self._close = kill, close
        self.pgid = pgid
        self.turn = _TURN.get()
        self.owner = _OWNER.get()
        self.started = time.monotonic()
        self.streak = 0
        self.stuck_reported = False
        self.clock = ProgressClock(pgid, self.size, stuck_s())
        self._cpu_at_look: float | None = None
        self.closed = False

    # ── output ──────────────────────────────────────────────────────────
    def size(self) -> int:
        return sum(sig.file_size(s) for s in self.streams)

    def _unread(self, advance: bool) -> list[str]:
        out = []
        for i, s in enumerate(self.streams):
            end = sig.file_size(s)
            start = self.seen[i]
            skipped = max(0, end - start - sig.SCAN_BYTES)
            text = sig.clean(sig.read_range(s, start + skipped, end))
            if skipped:
                text = f"… ({skipped} bytes not shown) …\n" + text
            out.append(text)
            if advance:
                self.seen[i] = end
        return out

    def _tail(self) -> str:
        """The last bit of the main stream — for prompt detection."""
        s = self.streams[0]
        end = sig.file_size(s)
        return sig.clean(sig.read_range(s, max(0, end - 400), end))

    def signal(self) -> str | None:
        return sig.signal_in("\n".join(self._unread(advance=False)),
                             self._tail())

    def alive(self) -> bool:
        return self.proc.poll() is None

    def cpu_active(self) -> bool | None:
        from aiforge_core.runtime.cmd_idle import group_cpu_s
        cpu = group_cpu_s(self.pgid) if self.alive() else None
        before, self._cpu_at_look = self._cpu_at_look, cpu
        if cpu is None:
            return None
        return cpu - (before or 0.0) >= 0.05

    def progress_token(self) -> str:
        """Changes whenever the job did anything — the loop guard keys a
        repeated wait on this, so waiting on a job that is working is never a
        loop, and waiting on one that is not, is."""
        from aiforge_core.runtime.cmd_idle import group_cpu_s
        cpu = group_cpu_s(self.pgid) if self.alive() else None
        return f"{self.size()}:{int(cpu or 0)}:{self.proc.poll()}"

    # ── lifecycle ───────────────────────────────────────────────────────
    def kill(self) -> None:
        if self.alive() and self._kill is not None:
            try:
                self._kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._close is not None:
            try:
                self._close()
            except Exception:  # noqa: BLE001
                pass


# ── the registry ─────────────────────────────────────────────────────────

def begin_turn():
    """Mark the jobs started from here on as this turn's. Returns the token
    for :func:`end_turn`."""
    token = object()
    return token, _TURN.set(token)


def end_turn(turn) -> int:
    """Kill this turn's handed-off jobs (explicit background ones keep
    running) and release the finished ones. Returns how many were killed."""
    token, ctx_tok = turn
    try:
        _TURN.reset(ctx_tok)
    except (ValueError, RuntimeError):
        pass
    killed = 0
    with _LOCK:
        mine = [j for j in _JOBS.values() if j.turn is token]
    for job in mine:
        if job.alive() and not job.explicit:
            job.kill()
            killed += 1
        if not job.alive():
            _forget(job)
    return killed


def current_owner():
    return _OWNER.get()


def set_owner(owner):
    """Tag jobs started from here on with ``owner``; returns the reset token."""
    return _OWNER.set(owner)


def reset_owner(tok) -> None:
    try:
        _OWNER.reset(tok)
    except (ValueError, RuntimeError):
        pass


def end_owner(owner) -> int:
    """Kill every live job ``owner`` started (explicit background included:
    the ticket they served is over). Returns how many were killed."""
    if owner is None:
        return 0
    with _LOCK:
        mine = [j for j in _JOBS.values() if j.owner == owner]
    killed = 0
    for job in mine:
        if job.alive():
            job.kill()
            killed += 1
        _forget(job)
    return killed


def progress_suffix(name: str, args) -> str:
    """Extra loop-guard key for a wait/peek: the job's progress. A repeated
    wait on a job that keeps producing output (or burning CPU) is a new call
    each time; one on a job that did nothing since is the same call again."""
    if name not in ("command_wait", "command_output"):
        return ""
    try:
        a = args if isinstance(args, dict) else {}
        job = find(a.get("id") if a.get("id") not in (None, "") else a.get("pid"))
        return "|" + (job.progress_token() if job is not None else "gone")
    except Exception:  # noqa: BLE001 — a guard must never break a call
        return ""


def _forget(job: Job) -> None:
    with _LOCK:
        _JOBS.pop(job.key, None)
    job.close()


def _register(job: Job) -> Job:
    with _LOCK:
        _JOBS[job.key] = job
        if len(_JOBS) > _MAX_JOBS:
            done = [j for j in _JOBS.values() if not j.alive()]
            for old in done[: len(_JOBS) - _MAX_JOBS]:
                _JOBS.pop(old.key, None)
                old.close()
    return job


def adopt_spooled(proc, spool, cmd: str, cwd: str, *, explicit: bool,
                  session_id=None, idle_s: float = 0.0,
                  deadline: float | None = None) -> Job:
    """Track a spooled shell command as a job (see bg_work.track_command for
    the Stop wiring and the end-of-run chat line)."""
    from aiforge_core.runtime import bg_work
    handle = bg_work.track_command(
        session_id, cwd, cmd, proc, spool, close_spool=False,
        announce=explicit, idle_s=idle_s, deadline=deadline,
        owner=_OWNER.get())
    key = handle.get("handle") or f"pid-{proc.pid}"

    def _kill():
        spool.kill_group()

    def _close():
        try:
            if not explicit:        # a bare `cmd &` inside dies with it
                spool.release_children()
        finally:
            spool.close()
    job = Job(key, proc, cmd, [spool.out, spool.err], session_id=session_id,
              explicit=explicit, kill=_kill, close=_close,
              pgid=handle.get("pgid") or proc.pid)
    job.handle = handle
    return _register(job)


def adopt_service(proc, cmd: str, log_path: str, pgid=None) -> Job:
    """A ``serve`` process: explicit background by nature, output in its log."""
    from aiforge_core.runtime.tools import serve as _serve

    def _kill():
        _serve.stop_service({"pid": proc.pid})
    job = Job(f"pid-{proc.pid}", proc, cmd, [log_path], explicit=True,
              kill=_kill, pgid=pgid)
    return _register(job)


def find(ref) -> Job | None:
    """By handle (``bg-7``), bare number (a bg id or a pid) or ``pid-123``."""
    s = str(ref or "").strip()
    with _LOCK:
        if s in _JOBS:
            return _JOBS[s]
        for key in (f"bg-{s}", f"pid-{s}"):
            if key in _JOBS:
                return _JOBS[key]
        for j in _JOBS.values():
            if str(getattr(j.proc, "pid", "")) == s:
                return j
    return None


def running() -> list[Job]:
    with _LOCK:
        return [j for j in _JOBS.values() if j.alive()]


# ── looking and waiting ──────────────────────────────────────────────────

def _hint(job: Job, alive: bool) -> str:
    if not alive:
        return "finished — the output above is final."
    return (f"still running. command_wait(id='{job.key}') to wait for more "
            f"(returns early on an error, a prompt or a stall), "
            f"command_output(id='{job.key}') to peek, command_kill(id="
            f"'{job.key}') to stop it and fix the command.")


def look(job: Job, why: str | None = None) -> dict:
    """What happened since the last look. Advances the look position."""
    alive = job.alive()
    parts = job._unread(advance=True)
    shown = [parts[0]] if parts[0] else []
    if len(parts) > 1 and parts[1]:
        shown.append("[stderr]\n" + parts[1])
    text = sig.bounded("\n".join(shown), TAIL_CHARS)
    out = {"ok": True, "id": job.key, "running": alive,
           "elapsed_s": round(time.monotonic() - job.started, 1),
           "new_output": text, "output_growing": bool(text)}
    if alive:
        out["cpu_active"] = job.cpu_active()
    else:
        code = job.proc.returncode
        out.update(ok=code == 0, code=code)
        _forget(job)
    if why:
        out["returned_because"] = why
    out["hint"] = _hint(job, alive)
    return out


def default_wait_s(job: Job) -> float:
    return min(300.0, 15.0 * (2 ** min(job.streak, 5)))


def wait(job: Job, max_s: float | None = None, session_id=None) -> dict:
    """Wait on ``job`` until it exits, shows a failure/prompt, looks stuck,
    or ``max_s`` passes — whichever is first."""
    from aiforge_core.runtime.run_interrupt import attention, steered
    limit = default_wait_s(job) if not max_s or max_s <= 0 else min(float(max_s), 600.0)
    end = time.monotonic() + limit
    while True:
        if not job.alive():
            return look(job, "exited")
        why = attention(session_id, only_replace=True) if session_id else None
        if why == "stop":
            job.kill()
            _forget(job)
            return {"ok": False, "stopped": True, "error": "stopped by user"}
        if why == "steer":
            return steered(id=job.key, hint=_hint(job, True))
        found = job.signal()
        if found:
            job.streak = 0
            return look(job, found)
        last = job.clock.last
        if job.clock.stalled():
            if not job.stuck_reported:
                job.stuck_reported, job.streak = True, 0
                res = look(job, f"looks stuck: no output and no CPU for "
                                f"{int(job.clock.idle_s)}s (waiting for input? "
                                "a network call? a lock?)")
                res["stuck"] = True
                return res
        elif job.clock.last != last:
            job.stuck_reported = False
        if time.monotonic() >= end:
            job.streak += 1
            return look(job, f"waited {int(limit)}s; still working")
        time.sleep(_POLL_S)


__all__ = ["Job", "adopt_service", "adopt_spooled", "begin_turn",
           "checkin_s", "current_owner", "default_wait_s", "end_owner",
           "end_turn", "find", "look", "progress_suffix", "reset_owner",
           "running", "set_owner", "stuck_s", "wait"]
