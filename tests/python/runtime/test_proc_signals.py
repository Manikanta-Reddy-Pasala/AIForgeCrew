"""One audited place decides what may be signalled.

Seven call sites reached for ``os.killpg`` / ``os.kill`` directly — the chat
shell, the Doer's bash session, the project runner, ``serve`` and the Stop
button. Each was killing something AIForge had started, each carried its own
bare ``except``, and a scanner asked the same question of all seven. The
argument for why it is safe now lives in one module — and is CHECKED there,
which is the part a comment could not do.
"""
from __future__ import annotations

import os
import signal

import pytest

from aiforge_core.runtime import proc_signals as ps


class _Proc:
    def __init__(self, pid=4242):
        self.pid = pid


# ── what it refuses ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("pgid", [0, 1, -1, None])
def test_a_group_that_would_take_the_app_down_is_refused(pgid, monkeypatch):
    """Group 0 is "everything in MY group" and pid 1 is init. Both are what a
    bad pid computation produces, and either one stops the app (or the
    container) along with the child."""
    sent: list = []
    monkeypatch.setattr(ps.os, "killpg", lambda *a: sent.append(a))
    assert ps.kill_group(pgid) is False
    assert sent == []


def test_our_own_process_group_is_refused(monkeypatch):
    """The value os.getpgid returns once the child has already been reaped."""
    sent: list = []
    monkeypatch.setattr(ps.os, "killpg", lambda *a: sent.append(a))
    assert ps.kill_group(os.getpgrp()) is False
    assert sent == []


def test_a_real_child_group_is_signalled(monkeypatch):
    sent: list = []
    monkeypatch.setattr(ps.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    assert ps.kill_group(999, signal.SIGTERM) is True
    assert sent == [(999, signal.SIGTERM)]


# ── never raises ────────────────────────────────────────────────────────────

def test_a_process_that_is_already_gone_is_not_an_error(monkeypatch):
    """A stop path runs while something has already gone wrong; the caller's
    next move is the same either way."""
    def _gone(*_a):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(ps.os, "killpg", _gone)
    monkeypatch.setattr(ps.os, "kill", _gone)
    assert ps.kill_group(999) is False
    assert ps.kill_process(999) is False


def test_group_of_survives_a_reaped_child(monkeypatch):
    def _boom(_pid):
        raise OSError("reaped")

    monkeypatch.setattr(ps.os, "getpgid", _boom)
    assert ps.group_of(_Proc()) is None


def test_group_of_reads_the_childs_group(monkeypatch):
    monkeypatch.setattr(ps.os, "getpgid", lambda pid: pid + 1)
    assert ps.group_of(_Proc(pid=10)) == 11
    assert ps.group_of(_Proc(pid=None)) is None


# ── the stop sequence ───────────────────────────────────────────────────────

def test_stop_asks_then_insists(monkeypatch):
    sent: list = []
    monkeypatch.setattr(ps.os, "killpg", lambda pgid, sig: sent.append(sig))
    assert ps.stop_group(999, pause_s=0.0) is True
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_stop_falls_back_to_the_pid_when_the_group_cannot_be_reached(monkeypatch):
    """A child that never became a group leader is still stopped."""
    killed: list = []

    def _no_group(*_a):
        raise ProcessLookupError("no group")

    monkeypatch.setattr(ps.os, "killpg", _no_group)
    monkeypatch.setattr(ps.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert ps.stop_group(999, pid=4242, pause_s=0.0) is True
    assert killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_kill_process_refuses_init_and_nothing(monkeypatch):
    sent: list = []
    monkeypatch.setattr(ps.os, "kill", lambda *a: sent.append(a))
    assert ps.kill_process(1) is False
    assert ps.kill_process(0) is False
    assert ps.kill_process(None) is False
    assert sent == []


# ── the callers go through it ───────────────────────────────────────────────

@pytest.mark.parametrize("module,attr", [
    ("aiforge_core.runtime.tools.bash", "_kill_group_and_reap"),
    ("aiforge_core.runtime.tools.serve", "_kill_pgid"),
    ("aiforge_core.runtime.tools.project_runner", "_kill"),
    ("aiforge_core.runtime.chat_agent._shell", "_kill_proc"),
])
def test_no_caller_signals_a_process_itself(module, attr):
    """Pin the WIRING: a shared guard nothing routes through guards nothing —
    the same failure shape as a gate that is defined and never attached."""
    import importlib
    import inspect

    fn = getattr(importlib.import_module(module), attr)
    src = inspect.getsource(fn)
    assert "os.killpg" not in src, f"{module}.{attr} still signals directly"
    assert "proc_signals" in src, f"{module}.{attr} does not use the helper"
