"""Hold the machine awake for as long as a run is in flight.

A team run or a scheduled job can take many minutes. Lock the screen and walk
away and the box idles into sleep, which suspends everything: the socket to the
model dies, the turn comes back as a transport error, and the work that had
already landed sits there waiting to be re-done. Nobody is served by an agent
that only finishes while someone watches it.

Screen LOCK is not the problem — every platform keeps running through it. SLEEP
is, and it is decided by the OS, not by this process. So each platform gets the
assertion it actually honours:

* macOS  — ``caffeinate -dims``, the same mechanism the operator would run by
  hand. ``-d``/``-i``/``-m``/``-s`` cover display, idle, disk and the AC-power
  system sleep.
* Linux  — ``systemd-inhibit --what=sleep:idle``, held by a child that does
  nothing until it is killed.
* WSL / Windows — Windows owns the power policy even when the process asking is
  inside the distro, so this reaches out through interop and calls
  ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` in a
  PowerShell child. No elevation: a Windows binary launched from WSL runs as
  the ordinary user, and this API needs nothing more.

Three properties this deliberately has:

* The assertion lives in a CHILD PROCESS, so it dies with the run even if this
  one is killed -9. A permanent power setting changed at the start of a task
  and restored at the end is a setting that stays changed when something
  crashes; an operator debugging why their laptop stopped sleeping a week later
  will not thank us.
* It never prevents the SCREEN from locking. Keeping a machine awake is a
  reasonable thing to ask; keeping it unlocked is not, and the two are
  separate assertions on every platform here.
* It is best-effort and silent. A box with no ``caffeinate``, no
  ``systemd-inhibit`` and no interop simply runs as it did before — a missing
  power API must never be the reason a ticket fails.

``AIFORGE_KEEP_AWAKE=0`` turns it off for an operator who wants the machine's
own policy to win.
"""
from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import subprocess
import threading

log = logging.getLogger("aiforge.keep_awake")

# ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001). NOT
# ES_DISPLAY_REQUIRED: the screen must still be free to lock.
_WIN_FLAGS = "0x80000001"

_PS_SCRIPT = (
    "$s = Add-Type -MemberDefinition '[DllImport(\"kernel32.dll\", "
    "SetLastError=true)] public static extern uint SetThreadExecutionState("
    "uint e);' -Name Pwr -Namespace AIForge -PassThru; "
    f"$s::SetThreadExecutionState({_WIN_FLAGS}) | Out-Null; "
    "while ($true) {{ Start-Sleep -Seconds 60 }}"
)

_lock = threading.Lock()
_holders = 0
_proc: "subprocess.Popen | None" = None


def enabled() -> bool:
    return os.environ.get("AIFORGE_KEEP_AWAKE", "1") not in ("0", "false", "no")


def _is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _command() -> "list[str] | None":
    """The child that HOLDS the assertion on this machine, or None."""
    system = platform.system()
    if system == "Darwin":
        if shutil.which("caffeinate"):
            # -s asserts only while on AC power, which is what a laptop owner
            # wants: a run must not flatten the battery of a closed laptop.
            return ["caffeinate", "-dims"]
        return None
    if system == "Windows" or _is_wsl():
        # From inside WSL the Windows binary is reached through interop; on
        # Windows proper it is simply on PATH. Same call either way.
        exe = shutil.which("powershell.exe") or shutil.which("powershell")
        if exe:
            return [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT]
        return None
    if system == "Linux" and shutil.which("systemd-inhibit"):
        return [
            "systemd-inhibit",
            "--what=sleep:idle",
            "--who=AIForge",
            "--why=a run is in flight",
            "--mode=block",
            "sleep", "infinity",
        ]
    return None


def _start_locked() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        return
    cmd = _command()
    if not cmd:
        return
    try:
        _proc = subprocess.Popen(          # noqa: S603 — fixed argv, no shell
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("keep_awake.acquired via %s", cmd[0])
    except Exception as exc:  # noqa: BLE001 — a power API must never fail a run
        _proc = None
        log.debug("keep_awake unavailable (%s): %s", cmd[0], exc)


def _stop_locked() -> None:
    global _proc
    p, _proc = _proc, None
    if p is None:
        return
    try:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        log.info("keep_awake.released")
    except Exception as exc:  # noqa: BLE001
        log.debug("keep_awake release failed: %s", exc)


def acquire() -> None:
    """Take a reference. Concurrent runs share ONE child — a per-run process
    would leave a fleet of them behind the first time one leaked."""
    global _holders
    if not enabled():
        return
    with _lock:
        _holders += 1
        if _holders == 1:
            _start_locked()


def release() -> None:
    """Drop a reference; the last one out releases the assertion. Never lets
    the count go negative: an unbalanced release would otherwise leave the next
    acquire unable to reach 1, and the machine would sleep under a live run."""
    global _holders
    with _lock:
        if _holders <= 0:
            _holders = 0
            return
        _holders -= 1
        if _holders == 0:
            _stop_locked()


@contextlib.contextmanager
def keep_awake(reason: str = ""):
    """Hold the machine awake for the duration of the block. Never raises."""
    acquired = False
    try:
        acquire()
        acquired = True
        if reason:
            log.debug("keep_awake: %s", reason)
    except Exception:  # noqa: BLE001
        acquired = False
    try:
        yield
    finally:
        if acquired:
            try:
                release()
            except Exception:  # noqa: BLE001
                pass


def active() -> bool:
    """Is an assertion currently held? (Test/diagnostic seam.)"""
    with _lock:
        return _proc is not None and _proc.poll() is None


__all__ = ["keep_awake", "acquire", "release", "active", "enabled"]
