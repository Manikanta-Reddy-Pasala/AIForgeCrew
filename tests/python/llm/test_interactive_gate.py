"""Background model traffic yields while someone is being served.

Measured on a live box: the memory learner made 95% of all model calls, in
bursts up to 436 calls long, and 25 of 53 interactive calls ran while a learner
call was in flight on the same endpoint.
"""
from __future__ import annotations

import time

import pytest

from aiforge_core.llm import interactive_gate as gate
from aiforge_core.llm import rate_limiter as rl


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_BACKGROUND_YIELD_S", "0.6")
    gate.reset()
    yield
    gate.reset()


def test_idle_means_no_wait(on):
    assert gate.busy_for() == 0.0
    t0 = time.monotonic()
    assert gate.yield_to_interactive(5.0) == 0.0
    assert time.monotonic() - t0 < 0.05


def test_background_waits_out_a_recent_interactive_send(on):
    gate.note_interactive()
    t0 = time.monotonic()
    waited = gate.yield_to_interactive(5.0)
    assert 0.4 <= waited <= 1.0
    assert 0.4 <= time.monotonic() - t0 <= 1.5


def test_the_wait_is_bounded_so_memory_is_delayed_never_starved(on, monkeypatch):
    monkeypatch.setenv("AIFORGE_BACKGROUND_YIELD_S", "30")
    gate.note_interactive()
    t0 = time.monotonic()
    waited = gate.yield_to_interactive(0.3)
    assert waited <= 0.35
    assert time.monotonic() - t0 < 1.0


def test_the_signal_crosses_processes_through_the_marker(on, tmp_path, monkeypatch):
    """The API and the runner are separate processes; a fresh module state
    (as in the other process) still sees the marker."""
    gate.note_interactive()
    monkeypatch.setattr(gate, "_last_local", 0.0)  # simulate the other process
    assert gate.busy_for() > 0
    assert (tmp_path / ".interactive-llm").exists()


def test_zero_disables_it(on, monkeypatch):
    monkeypatch.setenv("AIFORGE_BACKGROUND_YIELD_S", "0")
    gate.note_interactive()
    assert gate.busy_for() == 0.0
    assert gate.yield_to_interactive(5.0) == 0.0


# ── wired into the one gateway every send passes through ─────────────────────
def test_a_chat_send_marks_the_endpoint_busy(on):
    rl.acquire_global(role="chat", max_wait_s=1)
    assert gate.busy_for() > 0


def test_a_memory_send_waits_behind_a_chat_send(on):
    rl.acquire_global(role="chat", max_wait_s=1)
    t0 = time.monotonic()
    waited = rl.acquire_global(role="memory", max_wait_s=5)
    assert waited >= 0.4
    assert time.monotonic() - t0 >= 0.4


def test_a_chat_send_never_waits_behind_a_memory_send(on):
    rl.acquire_global(role="learner", max_wait_s=1)
    t0 = time.monotonic()
    assert rl.acquire_global(role="chat", max_wait_s=5) == 0.0
    assert time.monotonic() - t0 < 0.1


def test_pipeline_roles_count_as_interactive(on):
    """A team run is someone waiting too."""
    rl.acquire_global(role="planner", max_wait_s=1)
    assert gate.busy_for() > 0
