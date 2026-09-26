"""The persistent tmux ``bash`` tool, checked on instead of waited out.

``run_shell`` hands a command back as a job at the check-in
(``AIFORGE_CMD_CHECKIN_S``), on a failure line or an input prompt in its new
output (:mod:`aiforge_core.runtime.cmd_signals`), and ``command_wait`` then
reports it stuck after ``AIFORGE_CMD_STUCK_CHECK_S`` of silence. The tmux
``bash`` tool used to block until its own timeout instead. Here it gets the
same check-ins: a command still running at the check-in becomes a job in
:mod:`aiforge_core.runtime.cmd_jobs`, so ``command_wait`` / ``command_output``
/ ``command_kill`` work on it exactly as on a run_shell job.

The pane stays the session (cwd, env, venv persist). While a job runs in it
the pane is busy: another ``bash`` call is refused with the job's id rather
than typed into a running program. ``command_kill`` sends Ctrl-C, then kills
the pane's foreground process group if it ignores that.

The pane is a screen, not a stream. Each poll captures it, takes what lies
after the prompt that preceded the command (the echo stripped), and appends
the complete lines that are new to a spool file — the job's output stream. The
line still being written is kept out of the spool (a redrawn progress line
would otherwise be appended again on every poll) and read live for prompt
detection instead.
"""
from __future__ import annotations

import itertools
import os
import signal
import subprocess
import tempfile
import time
from typing import Any

from aiforge_core.runtime import cmd_jobs
from aiforge_core.runtime import cmd_signals as sig
from aiforge_core.runtime.cmd_idle import ProgressClock

_POLL_S = 0.1
_KILL_GRACE_S = 3.0
_SEQ = itertools.count(1)
#: pane name → the job running in it (the pane is busy until it ends).
_BUSY: dict[str, Any] = {}


def _b():
    from aiforge_core.runtime.tools import bash as _bash
    return _bash


def _capture(name: str) -> str | None:
    """The pane's text, or None when the session is gone."""
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", name, "-S", "-10000"],
        capture_output=True)
    if getattr(proc, "returncode", 0) not in (0, None):
        return None
    return (proc.stdout or b"").decode("utf-8", "replace")


def _tmux(*args: str) -> str:
    try:
        proc = subprocess.run(["tmux", *args], capture_output=True)
        return (proc.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _foreground_pgid(name: str) -> int | None:
    """The pane tty's foreground process group — the running command's."""
    pid = _tmux("display-message", "-p", "-t", name, "#{pane_pid}")
    if not pid.isdigit():
        return None
    try:
        out = subprocess.run(["ps", "-o", "tpgid=", "-p", pid],
                             capture_output=True)
        val = (out.stdout or b"").decode().strip()
    except Exception:  # noqa: BLE001
        return None
    if not val.lstrip("-").isdigit() or int(val) <= 0 or val == pid:
        return None
    return int(val)


class PaneRun:
    """A command typed into a pane, shaped like a ``Popen`` for cmd_jobs."""

    def __init__(self, name: str, command: str, n0: int) -> None:
        self.name, self.command = name, command
        self.n0 = n0                 # prompts on screen before the command
        self._count0 = n0
        self.returncode: int | None = None
        self.done = False
        self.body = ""               # the output so far (echo stripped)
        self.tail = ""               # the line still being written
        self.pid = None
        self.pgid: int | None = None
        fd, self.path = tempfile.mkstemp(prefix="aiforge-pane-", suffix=".log")
        os.close(fd)
        self._written = ""
        self.closed = False
        self._fg: tuple[float, int | None] = (float("-inf"), None)

    def current_pgid(self, max_age_s: float = 0.5) -> int | None:
        """The pane's foreground process group NOW. An interactive shell
        gives each command of ``a && b`` its own group, so the group seen at
        hand-off is dead once ``a`` ends; every check/kill resolves it again
        (cached for ``max_age_s`` — a poll 10x a second must not fork ``ps``
        10x a second). None when the shell is at its prompt."""
        at, pgid = self._fg
        now = time.monotonic()
        if self.done:
            return None
        if now - at > max_age_s:
            pgid = _foreground_pgid(self.name)
            self._fg = (now, pgid)
        return pgid

    # ── reading the pane ────────────────────────────────────────────────
    def refresh(self) -> None:
        if self.done:
            return
        pane = _capture(self.name)
        if pane is None:             # the session was destroyed under us
            self.done, self.returncode = True, -1
            self._spool(self.body, final=True)
            return
        rx = _b()._SENTINEL_RE
        matches = list(rx.finditer(pane))
        # History trimmed from the top: the prompts before ours go first.
        if len(matches) < self._count0:
            self.n0 = max(0, self.n0 - (self._count0 - len(matches)))
            self._count0 = len(matches)
        start = matches[self.n0 - 1].end() if self.n0 >= 1 else 0
        closing = matches[self.n0] if len(matches) > self.n0 else None
        finished = closing is not None and not pane[closing.end():].strip()
        text = pane[start:closing.start()] if finished else pane[start:]
        text = text.strip("\n")
        if self.n0 >= 1:
            stripped = _b()._strip_echoed_command(text, self.command)
            if stripped == text and self._echo_pending(text):
                stripped = ""        # only part of the echo so far
            text = stripped
        if finished:
            self.done, self.returncode = True, int(closing.group(1))
            self.body, self.tail = text, ""
            self._spool(text, final=True)
            return
        cut = text.rfind("\n")
        self.body, self.tail = text, text[cut + 1:]
        self._spool(text[:cut + 1] if cut >= 0 else "")

    def _echo_pending(self, text: str) -> bool:
        """The pty has not finished echoing the command yet."""
        target = "".join(self.command.split())
        got = "".join(text.split())
        return bool(target) and target.startswith(got)

    def _spool(self, text: str, final: bool = False) -> None:
        if self.closed:              # the job was forgotten: no new spool file
            return
        if final and text and not text.endswith("\n"):
            text += "\n"
        if text.startswith(self._written):
            new = text[len(self._written):]
        else:                        # the screen was redrawn: keep what is new
            anchor = self._written[-200:]
            at = text.rfind(anchor) if anchor else -1
            new = text[at + len(anchor):] if at >= 0 else text
        if not new:
            return
        self._written = text
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(new)
        except OSError:
            pass

    # ── Popen-like surface ──────────────────────────────────────────────
    def poll(self) -> int | None:
        self.refresh()
        return self.returncode if self.done else None

    def wait(self, timeout: float | None = None) -> int | None:
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.command, timeout)
            time.sleep(_POLL_S)
        return self.returncode

    def interrupt(self) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.name, "C-c"],
                       capture_output=True)

    def kill(self) -> None:
        """Ctrl-C; the CURRENT foreground group is killed if it ignores that
        (again for the next command of a chain, a few times at most)."""
        self.interrupt()
        for _ in range(3):
            end = time.monotonic() + _KILL_GRACE_S
            while self.poll() is None and time.monotonic() < end:
                time.sleep(_POLL_S)
            if self.done:
                return
            pgid = _foreground_pgid(self.name)
            if not pgid:
                continue             # between two commands: look again
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    def close(self) -> None:
        self.closed = True
        try:
            os.unlink(self.path)
        except OSError:
            pass


class _PaneClock(ProgressClock):
    """The stuck detector on the pane's CURRENT foreground group: a new
    group (the next command of ``a && b``) is progress, and its CPU is
    measured from its own start, not against the dead group's."""

    def __init__(self, run: "PaneRun", *a, **k) -> None:
        self._run = run
        super().__init__(run.current_pgid(), *a, **k)

    def _cpu_moved(self) -> bool:
        pgid = self._run.current_pgid()
        if pgid != self.pgid:
            self.pgid, self._cpu = pgid, None
            if pgid is not None:
                return True
        return super()._cpu_moved()


class PaneJob(cmd_jobs.Job):
    """A cmd_jobs job whose process is a command running in a tmux pane."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.clock = _PaneClock(self.proc, self.size, cmd_jobs.stuck_s())
        self._cpu_group: int | None = None

    @property
    def pgid(self) -> int | None:            # resolved on every check
        return self.proc.current_pgid()

    @pgid.setter
    def pgid(self, _value) -> None:          # Job.__init__ assigns it
        pass

    def cpu_active(self) -> bool | None:
        group = self.pgid
        if group != self._cpu_group:         # the chain moved on: progress
            self._cpu_group, self._cpu_at_look = group, None
            super().cpu_active()             # the new group's baseline
            return True if group is not None else None
        return super().cpu_active()

    def _tail(self) -> str:
        # The unfinished last line is not in the spool: read it live, so a
        # "Password:" / "[y/N]" prompt with no newline is still seen.
        return self.proc.tail

    def _unread(self, advance: bool) -> list[str]:
        # A pane capture cannot tell a finished last line from one still
        # being written, so the spool holds only the lines before it; show
        # that last line too (not advanced past: it comes again once it is
        # complete) — a quiet build's one status line is all there is to see.
        parts = super()._unread(advance)
        tail = self.proc.tail if not self.proc.done else ""
        if tail and parts:
            parts[0] = parts[0] + tail
        return parts


def busy(name: str):
    """The live job holding pane ``name``, else None. Held until the pane is
    back at its prompt — even after the job was forgotten (a kill that did
    not take), so the next ``bash`` is never typed into a running program."""
    job = _BUSY.get(name)
    if job is None:
        return None
    if job.alive():
        return job
    _BUSY.pop(name, None)
    return None


def _adopt(run: PaneRun, run_id: str) -> PaneJob:
    run.pgid = _foreground_pgid(run.name)
    run.pid = run.pgid

    def _close():
        if run.poll() is not None:   # back at the prompt: the pane is free
            _BUSY.pop(run.name, None)
        run.close()
    job = PaneJob(f"tmux-{next(_SEQ)}", run, run.command, [run.path],
                  kill=run.kill, close=_close, pgid=run.pgid)
    job.run_id = run_id
    _BUSY[run.name] = job
    return cmd_jobs._register(job)


def _stop_or_steer():
    """``"stop"`` / ``"steer"`` when the chat asks this command to end."""
    try:
        from aiforge_core.runtime import chat_cancel as _cc
        from aiforge_core.runtime.run_interrupt import attention as _why
        sid = _cc.active()
    except Exception:  # noqa: BLE001
        return None
    hit = _why(sid, only_replace=True)
    if hit in ("stop", "steer"):
        return hit
    if sid is not None and _cc.is_cancelled(sid):
        return "stop"
    return None


def run(name: str, command: str, run_id: str, wall_s: float) -> dict[str, Any]:
    """Type ``command`` into pane ``name``; return its result, or — still
    running at the check-in, or showing an error / a prompt — a job."""
    b = _b()
    subprocess.run(["tmux", "clear-history", "-t", name], capture_output=True)
    n0 = len(list(b._SENTINEL_RE.finditer(_capture(name) or "")))
    subprocess.run(["tmux", "send-keys", "-t", name, command, "Enter"],
                   check=True, capture_output=True)
    pane = PaneRun(name, command, n0)
    handed = False
    try:
        return _watch(pane, run_id, wall_s, b)
    except _HandedOff as h:
        handed = True
        return h.result
    finally:
        if not handed:
            pane.close()


class _HandedOff(Exception):
    def __init__(self, result: dict) -> None:
        super().__init__("handed off")
        self.result = result


def _watch(pane: PaneRun, run_id: str, wall_s: float, b) -> dict[str, Any]:
    checkin = cmd_jobs.checkin_s()
    now = time.monotonic()
    checkin_at = now + checkin if checkin > 0 else None
    deadline = now + wall_s if wall_s > 0 else None
    scanned = 0
    cap = b._STDOUT_CAP_BYTES
    while True:
        hit = _stop_or_steer()
        if hit:
            pane.interrupt()
            if hit == "steer":
                from aiforge_core.runtime.run_interrupt import steered
                return steered(command=pane.command, stdout=pane.body[:cap])
            return b._err_result(pane.command, "stopped by user",
                                 stopped=True, stdout=pane.body[:cap])
        pane.refresh()
        if pane.done:
            rc = pane.returncode
            return {"ok": rc == 0, "returncode": rc, "command": pane.command,
                    "stdout": pane.body[:cap], "truncated": len(pane.body) > cap}
        if deadline is not None and time.monotonic() > deadline:
            pane.interrupt()
            try:
                pane.wait(2)
            except subprocess.TimeoutExpired:
                pass
            return b._err_result(pane.command, "timeout",
                                 stdout=pane.body[:cap], truncated=True)
        why = None
        if len(pane.body) != scanned:
            new = pane.body[scanned:] if len(pane.body) > scanned else pane.body
            scanned = len(pane.body)
            why = sig.signal_in(new[-sig.SCAN_BYTES:], pane.tail)
        if why is None and checkin_at is not None \
                and time.monotonic() >= checkin_at:
            why = "check-in: still running"
        if why:
            job = _adopt(pane, run_id)
            res = cmd_jobs.look(job, why)
            res["command"] = pane.command
            raise _HandedOff(res)
        time.sleep(_POLL_S)


__all__ = ["PaneJob", "PaneRun", "busy", "run"]
