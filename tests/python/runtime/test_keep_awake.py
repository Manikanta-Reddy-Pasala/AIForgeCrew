"""Hold the machine awake while a run is in flight.

A team run or a scheduled job is minutes of work that usually happens with
nobody at the keyboard. If the box idles into sleep, the process is suspended:
the model socket dies and everything already done waits to be re-done. Screen
LOCK is fine on every platform — SLEEP is the thing to hold off, and it is the
OS's decision, so each platform gets the assertion it actually honours.
"""
from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime import keep_awake as ka


class _FakeProc:
    def __init__(self, *_a, **_k):
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ka._holders = 0
    ka._proc = None
    monkeypatch.delenv("AIFORGE_KEEP_AWAKE", raising=False)
    yield
    ka._holders = 0
    ka._proc = None


def _spawned(monkeypatch):
    made: list = []

    def _popen(cmd, **_k):
        made.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(ka.subprocess, "Popen", _popen)
    return made


def test_the_assertion_is_held_for_the_block_and_dropped_after(monkeypatch):
    made = _spawned(monkeypatch)
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    assert not ka.active()
    with ka.keep_awake("a run"):
        assert ka.active()
        assert made == [["caffeinate", "-dims"]]
    assert not ka.active()


def test_concurrent_runs_share_one_child(monkeypatch):
    """A child per run leaves a fleet of them behind the first time one leaks,
    and every one of them keeps a laptop awake."""
    made = _spawned(monkeypatch)
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    with ka.keep_awake("run A"):
        with ka.keep_awake("run B"):
            assert len(made) == 1
        assert ka.active(), "the inner run ending must not wake the machine"
    assert not ka.active()


def test_an_unbalanced_release_cannot_go_negative(monkeypatch):
    """A negative count would leave the NEXT acquire unable to reach 1 — the
    machine sleeping under a live run, which is the whole failure."""
    _spawned(monkeypatch)
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    ka.release()
    ka.release()
    assert ka._holders == 0
    with ka.keep_awake("a run"):
        assert ka.active()


def test_a_box_with_no_power_api_still_runs(monkeypatch):
    """A missing caffeinate / systemd-inhibit / interop must never be the
    reason a ticket fails."""
    monkeypatch.setattr(ka, "_command", lambda: None)
    with ka.keep_awake("a run"):
        assert not ka.active()          # nothing held, and nothing raised


def test_a_spawn_failure_is_not_fatal(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("no fork for you")

    monkeypatch.setattr(ka.subprocess, "Popen", _boom)
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    with ka.keep_awake("a run"):
        assert not ka.active()


def test_the_operator_can_turn_it_off(monkeypatch):
    made = _spawned(monkeypatch)
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    monkeypatch.setenv("AIFORGE_KEEP_AWAKE", "0")
    with ka.keep_awake("a run"):
        assert made == []


def test_macos_asks_for_sleep_not_for_an_unlocked_screen(monkeypatch):
    monkeypatch.setattr(ka.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ka.shutil, "which", lambda n: "/usr/bin/caffeinate")
    assert ka._command()[0] == "caffeinate"


def test_wsl_reaches_windows_because_windows_owns_the_policy(monkeypatch):
    """Inside WSL the distro cannot decide anything about power — Windows can,
    and a Windows binary launched from WSL runs as the ordinary user, so this
    needs no elevation."""
    monkeypatch.setattr(ka.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ka, "_is_wsl", lambda: True)
    monkeypatch.setattr(ka.shutil, "which",
                        lambda n: "/mnt/c/.../powershell.exe"
                        if n.startswith("powershell") else None)
    cmd = ka._command()
    assert cmd[0].endswith("powershell.exe")
    script = cmd[-1]
    assert "SetThreadExecutionState" in script
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED — and NOT ES_DISPLAY_REQUIRED (0x2):
    # keeping a machine awake is reasonable, keeping it unlocked is not.
    assert "0x80000001" in script
    assert "Start-Sleep -Seconds 60 }" in script      # the f-string braces survived


def test_plain_linux_uses_the_inhibitor(monkeypatch):
    monkeypatch.setattr(ka.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ka, "_is_wsl", lambda: False)
    monkeypatch.setattr(ka.shutil, "which",
                        lambda n: "/usr/bin/systemd-inhibit"
                        if n == "systemd-inhibit" else None)
    cmd = ka._command()
    assert cmd[0] == "systemd-inhibit" and "--what=sleep:idle" in cmd


def test_a_child_that_ignores_terminate_is_killed(monkeypatch):
    class _Stubborn(_FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("caffeinate", timeout or 5)

    monkeypatch.setattr(ka.subprocess, "Popen", lambda *a, **k: _Stubborn())
    monkeypatch.setattr(ka, "_command", lambda: ["caffeinate", "-dims"])
    ka.acquire()
    proc = ka._proc
    ka.release()
    assert proc.killed, "a stuck assertion would keep the machine awake forever"
