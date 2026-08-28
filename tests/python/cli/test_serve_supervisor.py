"""The launcher every installer calls.

The .deb, the .app and the .msi all run `aiforge` — so what this module does on
start and, more importantly, on the way OUT is the contract three packages
depend on. Nothing here starts uvicorn or the real loops; the supervisor is
driven directly with a fake child so the test is fast and hermetic.
"""
from __future__ import annotations

import time

import pytest

from aiforge_core.cli import serve


class _FakeProc:
    """Stands in for a background pass: alive until asked to stop."""

    def __init__(self, *, ignores_terminate: bool = False):
        self.alive = True
        self.terminated = False
        self.killed = False
        self._ignores = ignores_terminate

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        if not self._ignores:
            self.alive = False

    def wait(self, timeout=None):
        if self.alive:
            import subprocess
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return 0

    def kill(self):
        self.killed = True
        self.alive = False


def test_both_loops_are_supervised_by_default(monkeypatch):
    spawned = []
    monkeypatch.setattr(serve._Supervisor, "_spawn",
                        lambda self, module: spawned.append(module) or _FakeProc())
    sup = serve._Supervisor()
    started = [m for m, _e, _d in sup.start()]
    try:
        assert started == ["aiforge_core.runtime.adk_runner",
                           "aiforge_core.memory.sync.loop"]
    finally:
        sup.stop()


def test_a_skipped_loop_is_never_started(monkeypatch):
    """--no-sync has to mean the sync loop does not run, not that it runs
    quietly."""
    spawned = []
    monkeypatch.setattr(serve._Supervisor, "_spawn",
                        lambda self, module: spawned.append(module) or _FakeProc())
    sup = serve._Supervisor()
    started = [m for m, _e, _d in sup.start({"aiforge_core.memory.sync.loop"})]
    try:
        assert started == ["aiforge_core.runtime.adk_runner"]
        time.sleep(0.2)
        assert "aiforge_core.memory.sync.loop" not in spawned
    finally:
        sup.stop()


def test_stop_kills_the_children_it_started(monkeypatch):
    """The whole point: closing the app must not leave three orphans polling."""
    procs = []

    def _spawn(self, module):
        p = _FakeProc()
        procs.append(p)
        self._procs.append(p)
        return p

    monkeypatch.setattr(serve._Supervisor, "_spawn", _spawn)
    sup = serve._Supervisor()
    sup.start()
    for _ in range(40):           # let both threads get a child up
        if len(procs) >= 2:
            break
        time.sleep(0.05)
    sup.stop()
    assert len(procs) >= 2
    assert all(p.terminated for p in procs)
    assert all(not p.alive for p in procs)


def test_a_child_that_ignores_terminate_is_killed():
    """A wedged pass (a network read that never returns) does not get to keep
    the app alive."""
    proc = _FakeProc(ignores_terminate=True)
    serve._terminate(proc)
    assert proc.terminated
    assert proc.killed


def test_terminate_leaves_an_already_dead_child_alone():
    proc = _FakeProc()
    proc.alive = False
    serve._terminate(proc)
    assert not proc.terminated


def test_stop_is_safe_twice(monkeypatch):
    """It is called from the signal handler AND the finally block."""
    monkeypatch.setattr(serve._Supervisor, "_spawn",
                        lambda self, module: _FakeProc())
    sup = serve._Supervisor()
    sup.start()
    time.sleep(0.1)
    sup.stop()
    sup.stop()


def test_a_loop_that_cannot_start_does_not_spin(monkeypatch, capsys):
    """A missing module must not become a hot loop of failed spawns."""
    calls = []

    def _boom(self, module):
        calls.append(module)
        print(f"  ! could not start {module}: no", flush=True)
        return None

    monkeypatch.setattr(serve._Supervisor, "_spawn", _boom)
    monkeypatch.setenv("AIFORGE_RUNNER_POLL_SEC", "30")
    monkeypatch.setenv("AIFORGE_SYNC_POLL_SEC", "30")
    sup = serve._Supervisor()
    sup.start()
    time.sleep(0.3)
    sup.stop()
    # One attempt each, then the poll interval — not a spin.
    assert len(calls) <= 2


@pytest.mark.parametrize("host,expect_warning", [
    ("127.0.0.1", False),
    ("0.0.0.0", True),
])
def test_binding_off_loopback_without_a_token_is_called_out(
        host, expect_warning, monkeypatch, capsys):
    """The installers put "listen on the network" one checkbox away."""
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    serve._announce(host, 8799)
    err = capsys.readouterr().err
    assert ("AIFORGE_API_TOKEN" in err) is expect_warning


def test_the_warning_goes_away_once_a_token_is_set(monkeypatch, capsys):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "x")
    serve._announce("0.0.0.0", 8799)
    assert "AIFORGE_API_TOKEN" not in capsys.readouterr().err


def test_poll_intervals_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AIFORGE_RUNNER_POLL_SEC", "45")
    assert serve._env_int("AIFORGE_RUNNER_POLL_SEC", 10) == 45
    monkeypatch.setenv("AIFORGE_RUNNER_POLL_SEC", "0")
    assert serve._env_int("AIFORGE_RUNNER_POLL_SEC", 10) == 10
    monkeypatch.setenv("AIFORGE_RUNNER_POLL_SEC", "nonsense")
    assert serve._env_int("AIFORGE_RUNNER_POLL_SEC", 10) == 10


def test_the_url_is_one_you_can_click():
    """0.0.0.0 is not an address a human can open — and the URL is built in one
    place so the printed one and the opened one cannot drift apart."""
    assert serve.ui_url("0.0.0.0", 8799) == "http://localhost:8799/ui/"
    assert serve.ui_url("127.0.0.1", 8799) == "http://localhost:8799/ui/"
    assert serve.ui_url("192.168.1.10", 9000) == "http://192.168.1.10:9000/ui/"
