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
    ka._beat = None
    ka._beat_path = None
    monkeypatch.delenv("AIFORGE_KEEP_AWAKE", raising=False)
    monkeypatch.delenv("AIFORGE_SANDBOX", raising=False)
    monkeypatch.delenv("AIFORGE_AWAKE_FILE", raising=False)
    yield
    if ka._beat is not None:
        ka._beat.set()
    ka._holders = 0
    ka._proc = None
    ka._beat = None


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
    assert cmd[0] == "systemd-inhibit"
    assert "--what=sleep:idle" in cmd


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


def test_in_the_sandbox_a_heartbeat_file_asks_the_host_to_hold(monkeypatch, tmp_path):
    """The docker box has no power API of the host's: it keeps a heartbeat
    file in the mounted config dir that scripts/awake-bridge.sh on the host
    holds the assertion for, and removes it when the last run ends."""
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ka, "_command", lambda: None)
    beat = tmp_path / "run" / "keep-awake"
    with ka.keep_awake("a run"):
        assert beat.exists() and ka.active()
        with ka.keep_awake("a second run"):
            assert beat.exists()
        assert beat.exists()               # the first run is still going
    assert not beat.exists() and not ka.active()


def test_outside_the_sandbox_no_heartbeat_file_is_written(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ka, "_command", lambda: None)
    with ka.keep_awake("a run"):
        assert not (tmp_path / "run").exists()


def test_in_the_sandbox_the_file_wins_over_a_box_local_power_api(monkeypatch, tmp_path):
    """A systemd-inhibit apt-installed in the box cannot reach the host."""
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    made = _spawned(monkeypatch)
    monkeypatch.setattr(ka, "_command", lambda: ["systemd-inhibit", "sleep", "infinity"])
    with ka.keep_awake("a run"):
        assert (tmp_path / "run" / "keep-awake").exists()
    assert made == [] and not (tmp_path / "run" / "keep-awake").exists()


def test_release_removes_the_file_it_wrote_even_if_the_env_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "a"))
    monkeypatch.setattr(ka, "_command", lambda: None)
    ka.acquire()
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "b"))
    ka.release()
    assert not (tmp_path / "a" / "run" / "keep-awake").exists()


# ─── the host bridge's own mark ─────────────────────────────────────────

def test_host_holding_reads_the_bridge_mark(tmp_path, monkeypatch):
    import os
    import time as _t
    f = tmp_path / "keep-awake"
    monkeypatch.setenv("AIFORGE_AWAKE_FILE", str(f))
    assert ka.host_holding() is False                 # no mark
    (tmp_path / "keep-awake.held").write_text("")
    assert ka.host_holding() is True
    old = _t.time() - 600
    os.utime(tmp_path / "keep-awake.held", (old, old))
    assert ka.host_holding() is False                 # stale: the bridge is gone
    monkeypatch.delenv("AIFORGE_AWAKE_FILE")
    assert ka.host_holding() is None                  # not the sandbox


def test_a_run_with_no_host_hold_is_reported_once(tmp_path, monkeypatch):
    import time as _t
    monkeypatch.setenv("AIFORGE_AWAKE_FILE", str(tmp_path / "keep-awake"))
    monkeypatch.setattr(ka, "BEAT_S", 0.02)
    monkeypatch.setattr(ka, "UNHELD_AFTER_S", 0.05)
    said = []

    def report(told):
        said.append(1)
        told.add("chat")              # told: not again for this hold
        return 1
    monkeypatch.setattr(ka, "_report_unheld", report)
    ka.acquire()
    end = _t.monotonic() + 3
    while not said and _t.monotonic() < end:
        _t.sleep(0.02)
    ka.release()
    assert said                       # reported, and only while a chat was untold


def test_a_held_run_is_not_reported(tmp_path, monkeypatch):
    import time as _t
    monkeypatch.setenv("AIFORGE_AWAKE_FILE", str(tmp_path / "keep-awake"))
    (tmp_path / "keep-awake.held").write_text("")
    monkeypatch.setattr(ka, "BEAT_S", 0.02)
    monkeypatch.setattr(ka, "UNHELD_AFTER_S", 0.05)
    said = []
    monkeypatch.setattr(ka, "_report_unheld", lambda told: said.append(1))
    ka.acquire()
    _t.sleep(0.3)
    ka.release()
    assert said == []


def test_the_report_reaches_running_chats(monkeypatch):
    from aiforge_core.runtime import chat_runs
    got = []
    answered = {6}
    monkeypatch.setattr(chat_runs, "note", lambda sid, ev: (
        got.append((sid, ev)) or True) if sid not in answered else False)

    class R:
        done = False
    monkeypatch.setattr(chat_runs, "_RUNS", {5: R(), 6: R()})
    told = set()
    assert ka._report_unheld(told) == 1
    assert told == {5} and got[0][0] == 5 and "awake" in got[0][1]["text"]
    assert "AIFORGE_KEEP_AWAKE=0" in got[0][1]["text"]
    answered.clear()                  # 6 not told yet: a later pass tells it
    assert ka._report_unheld(told) == 1 and told == {5, 6}
    assert ka._report_unheld(told) == 0
