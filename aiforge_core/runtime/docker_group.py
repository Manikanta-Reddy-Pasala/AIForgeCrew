"""A sandbox command's processes INSIDE its container.

``docker exec`` runs the command in the container; on the host there is only
the ``docker exec`` CLIENT, which uses no CPU while the command compiles and
which, killed, leaves the command running in the container (no ``-t``: no
hang-up reaches it). So every process-group operation the shell paths do on
the host group — the idle detector's CPU reading, Stop, ``command_kill``, the
hung / wall-clock kill — must reach the container instead.

Each sandbox command is started with ``AIFORGE_JOB=<key>`` in its
environment (``docker exec -e``). Every process it starts inherits it, so the
command's processes are found in the container by that marker in
``/proc/<pid>/environ`` — even ones that started their own session. A
:class:`RemoteGroup` is registered under the host client's process group
(:func:`register`); :func:`aiforge_core.runtime.cmd_idle.group_cpu_s` and
:func:`aiforge_core.runtime.proc_signals.kill_group` consult :func:`lookup`
first, so run_to_completion, the job table, the background watcher and the
chat Stop registry all work on the in-container processes unchanged.
"""
from __future__ import annotations

import os
import signal as _signal
import subprocess
import threading
import time
import uuid

ENV = "AIFORGE_JOB"
#: A housekeeping ``docker exec`` never holds up a stop or a check for long.
_TIMEOUT_S = 10.0
_GRACE_S = 1.0

# $1 = what (stat | a signal name), $2 = the job key.
_SCRIPT = (
    'for d in /proc/[0-9]*; do '
    'tr "\\000" "\\n" < "$d/environ" 2>/dev/null | grep -qx "AIFORGE_JOB=$2" '
    '|| continue; '
    'if [ "$1" = stat ]; then cat "$d/stat" 2>/dev/null; echo; '
    'else kill -s "$1" "${d#/proc/}" 2>/dev/null; fi; '
    'done; true')

_GROUPS: dict[int, "RemoteGroup"] = {}
_LOCK = threading.Lock()


def _docker(args: list[str], timeout: float = _TIMEOUT_S):
    """One housekeeping ``docker`` call (patched by the tests)."""
    return subprocess.run(["docker", *args], capture_output=True,
                          timeout=timeout)


def _tick() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100.0


class RemoteGroup:
    """The processes of one sandbox command in container ``container``."""

    def __init__(self, container: str, key: str | None = None) -> None:
        self.container = container
        self.key = key or uuid.uuid4().hex[:16]
        self.local_pgid: int | None = None

    def argv(self, command: str) -> list[str]:
        """The ``docker exec`` that runs ``command`` marked with this key."""
        return ["docker", "exec", "-i", "-e", f"{ENV}={self.key}",
                self.container, "bash", "-lc", command]

    def _run(self, what: str, runner=None) -> str | None:
        try:
            proc = (runner or _docker)([
                "exec", self.container, "sh", "-c", _SCRIPT,
                "aiforge-group", what, self.key])
        except Exception:  # noqa: BLE001 — docker gone / hung: cannot say
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        out = proc.stdout or b""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) \
            else str(out)

    def cpu_s(self) -> float | None:
        """CPU seconds used by the command's processes (their reaped
        children included), or None when none is left / docker cannot say."""
        text = self._run("stat")
        if text is None:
            return None
        total, seen = 0.0, False
        for line in text.splitlines():
            if ")" not in line:
                continue
            fields = line.rsplit(")", 1)[1].split()
            try:
                total += sum(int(f) for f in fields[11:15])
                seen = True
            except (ValueError, IndexError):
                continue
        return total / _tick() if seen else None

    def signal(self, sig: int) -> bool:
        """Send ``sig`` to the command's processes in the container — on a
        thread, so Stop never waits on a slow ``docker exec``. A TERM is
        followed by a KILL after a grace (the caller's own KILL may never
        come: the host client is gone once it got the TERM)."""
        name = _signal.Signals(sig).name[3:] if sig else "0"
        todo = [name] + (["KILL"] if name == "TERM" else [])
        runner = _docker

        def run():
            for i, what in enumerate(todo):
                if i:
                    time.sleep(_GRACE_S)
                self._run(what, runner)
        threading.Thread(target=run, daemon=True,
                         name=f"docker-signal-{self.key}").start()
        return True

    def stop(self, pause_s: float = 0.5) -> None:
        """TERM, then KILL (blocking: tests and teardown)."""
        self._run("TERM")
        time.sleep(pause_s)
        self._run("KILL")


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def register(local_pgid: int, group: RemoteGroup) -> None:
    """``group`` is what the host process group ``local_pgid`` stands for,
    until :func:`release_when_done` sees the host client end."""
    group.local_pgid = local_pgid
    with _LOCK:
        _GROUPS[local_pgid] = group


def release_when_done(proc, group: RemoteGroup) -> None:
    """Drop ``group``'s entry once its host client ``proc`` has ended — the
    moment its process-group id could be reused by something unrelated."""
    def run():
        while proc.poll() is None:
            time.sleep(0.5)
        unregister(group.local_pgid)
    threading.Thread(target=run, daemon=True,
                     name=f"docker-group-{group.key}").start()


def unregister(local_pgid: int | None) -> None:
    if local_pgid is None:
        return
    with _LOCK:
        _GROUPS.pop(local_pgid, None)


def lookup(pgid: int | None) -> RemoteGroup | None:
    if pgid is None:
        return None
    with _LOCK:
        return _GROUPS.get(pgid)


def _reset_for_tests() -> None:
    with _LOCK:
        _GROUPS.clear()


__all__ = ["ENV", "RemoteGroup", "lookup", "register", "release_when_done",
           "unregister"]
