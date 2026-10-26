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

Inside the docker sandbox none of these reach the host: the box is Linux with
no systemd, and a Mac that sleeps pauses the whole Docker VM under the run.
There the hold is a HEARTBEAT FILE in the mounted config dir
(``<config>/run/keep-awake``, touched every 20 s while a run is in flight,
removed after it), and ``scripts/awake-bridge.sh`` — started on the host by
``./run.sh`` — holds the host's own assertion while the file is fresh. A box
killed -9 stops touching the file, so the host lets go within a minute.

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
import time

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
#: Heartbeat period of the sandbox file; the host bridge treats a file older
#: than three of these as released.
BEAT_S = 20
_beat: "threading.Event | None" = None
_beat_path: "str | None" = None
#: Serialises a heartbeat touch against the release's remove: a beat that
#: woke just as the run ended must not write the file back after it is gone.
_beat_lock = threading.Lock()
#: How long a run goes before a missing host hold is reported (the bridge
#: checks every 15 s).
UNHELD_AFTER_S = 50
#: The bridge's own mark is fresh for this long.
HELD_FRESH_S = 60


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


def awake_file() -> "str | None":
    """The heartbeat file a host bridge watches, or None outside the sandbox.
    ``AIFORGE_AWAKE_FILE`` names it explicitly."""
    explicit = os.environ.get("AIFORGE_AWAKE_FILE", "").strip()
    if explicit:
        return explicit
    if os.environ.get("AIFORGE_SANDBOX", "") != "1":
        return None
    try:
        from aiforge_core.config.paths import config_dir
        return os.path.join(str(config_dir()), "run", "keep-awake")
    except Exception:  # noqa: BLE001
        return None


def _touch(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
    except OSError as exc:
        log.debug("keep_awake file %s: %s", path, exc)


def _start_file_locked(path: str) -> None:
    global _beat, _beat_path
    if _beat is not None:
        return
    stop = threading.Event()
    _beat, _beat_path = stop, path
    _touch(path)

    started = time.monotonic()
    warned: set = set()          # chats already told, for this hold
    misses = [0]

    def beat():
        while not stop.wait(BEAT_S):
            with _beat_lock:
                if not stop.is_set():
                    _touch(path)
            if stop.is_set():
                continue
            # Two misses a beat apart: right after a wake the bridge's mark
            # is old until its next pass, and that is no reason to warn.
            misses[0] = misses[0] + 1 if host_holding(path) is False else 0
            if misses[0] >= 2 and time.monotonic() - started >= UNHELD_AFTER_S:
                _report_unheld(warned)
    threading.Thread(target=beat, daemon=True, name="keep-awake-beat").start()
    log.info("keep_awake.acquired via %s (host bridge)", path)


def host_holding(path: "str | None" = None) -> "bool | None":
    """Does the host bridge hold this machine awake right now? None outside
    the sandbox (a hold there is this process's own child)."""
    path = path or awake_file()
    if not path:
        return None
    try:
        return time.time() - os.path.getmtime(path + ".held") <= HELD_FRESH_S
    except OSError:
        return False


UNHELD_TEXT = ("⚠️ Nothing is keeping this computer awake while the agent "
               "works: if it sleeps (screen locked, lid closed), the run "
               "pauses until it wakes. Start AIForge with ./run.sh on this "
               "computer (it starts the keep-awake helper), or turn sleep off. "
               "Set AIFORGE_KEEP_AWAKE=0 to stop this note.")


def _report_unheld(told: "set | None" = None) -> int:
    """Tell each running chat not in ``told`` (added to it) that nothing
    holds this machine awake. Returns how many were told."""
    told = set() if told is None else told
    n = 0
    try:
        from aiforge_core.runtime import chat_runs
        with chat_runs._LOCK:
            live = [sid for sid, r in chat_runs._RUNS.items() if not r.done]
        for sid in live:
            if sid in told:
                continue
            # note(): said beside the run (its phase untouched) and kept
            # with the turn's steps.
            if chat_runs.note(sid, {"type": "message", "role": "system",
                                    "supplementary": True, "text": UNHELD_TEXT}):
                told.add(sid)
                n += 1
    except Exception:  # noqa: BLE001 — a warning must never break a run
        pass
    if n:
        log.warning("keep_awake: a run is in flight but the host bridge does "
                    "not hold this machine awake (start it with ./run.sh)")
    return n


def _stop_file_locked() -> None:
    global _beat, _beat_path
    stop, path, _beat, _beat_path = _beat, _beat_path, None, None
    if stop is None:
        return
    with _beat_lock:
        stop.set()
        if path:                    # the file this hold wrote, whatever the env says now
            try:
                os.remove(path)
            except OSError:
                pass
    log.info("keep_awake.released (host bridge)")


def _start_locked() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        return
    # In the sandbox no process of the box can hold the HOST awake (a
    # systemd-inhibit installed there has no host D-Bus): the heartbeat file
    # is the only hold that works, whatever _command() finds.
    path = awake_file()
    if path:
        _start_file_locked(path)
        return
    cmd = _command()
    if not cmd:
        return
    try:
        _proc = subprocess.Popen(          # noqa: S603
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
    _stop_file_locked()
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
        return (_proc is not None and _proc.poll() is None) or _beat is not None


__all__ = ["keep_awake", "acquire", "release", "active", "enabled", "awake_file",
           "host_holding"]
