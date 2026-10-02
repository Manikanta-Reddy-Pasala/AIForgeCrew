"""Learner and memory calls wait for an active chat run on a one-slot server."""
import pytest

from aiforge_core.llm import background_gate as G


class _Runs:
    def __init__(self, active_polls):
        self.n = active_polls

    def any_active(self):
        self.n -= 1
        return self.n >= 0


@pytest.fixture
def wired(monkeypatch):
    from aiforge_core.llm import slots
    from aiforge_core.runtime import chat_runs
    monkeypatch.setenv("AIFORGE_BACKGROUND_YIELD_S", "30")
    monkeypatch.setattr(G, "_POLL_S", 0.0)
    monkeypatch.setattr(slots, "parallel_ok", lambda *a: False)
    return chat_runs, slots, monkeypatch


def test_a_background_role_waits_until_the_chat_run_ends(wired):
    chat_runs, _slots, mp = wired
    runs = _Runs(3)
    mp.setattr(chat_runs, "any_active", runs.any_active)
    G.wait_for_foreground("learner")
    assert runs.n < 0                     # polled until it reported idle


def test_other_roles_and_roomy_servers_never_wait(wired):
    chat_runs, slots, mp = wired
    runs = _Runs(100)
    mp.setattr(chat_runs, "any_active", runs.any_active)
    assert G.wait_for_foreground("doer") == 0.0
    mp.setattr(slots, "parallel_ok", lambda *a: True)
    assert G.wait_for_foreground("learner") == 0.0
    assert runs.n == 100


def test_zero_turns_it_off_and_the_wait_is_bounded(wired):
    chat_runs, _s, mp = wired
    mp.setattr(chat_runs, "any_active", lambda: True)
    mp.setenv("AIFORGE_BACKGROUND_YIELD_S", "0")
    assert G.wait_for_foreground("learner") == 0.0
    mp.setenv("AIFORGE_BACKGROUND_YIELD_S", "0.05")
    mp.setattr(G, "_POLL_S", 0.01)
    assert 0.04 <= G.wait_for_foreground("learner") < 1.0
