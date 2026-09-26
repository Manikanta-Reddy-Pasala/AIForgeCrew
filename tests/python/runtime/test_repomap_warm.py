"""The ranked repo map is built in the background from session/turn open; the
first turn waits for it only briefly and falls back to the quick map, and the
parse it left running serves the next turn instead of being started again."""
from __future__ import annotations

import threading

import pytest

from aiforge_core.memory import code_context as cc
from aiforge_core.runtime.chat_agent._context import _repomap as rm


@pytest.fixture
def slow_digest(monkeypatch, tmp_path):
    gate = threading.Event()
    calls = []

    def _digest(base, files):
        calls.append(base)
        gate.wait(10)
        return "a.py: main"
    monkeypatch.setattr(cc, "aider_digest", _digest)
    monkeypatch.setattr(rm, "_workspace_root", lambda: None)
    monkeypatch.setattr(rm, "_WARM", {})
    monkeypatch.setenv("AIFORGE_REPOMAP_BUDGET_S", "0.2")
    (tmp_path / "a.py").write_text("def main():\n    pass\n")
    yield gate, calls, str(tmp_path)
    gate.set()


def test_a_slow_first_parse_does_not_block_the_turn(slow_digest):
    gate, calls, base = slow_digest
    assert rm._aider_digest_bounded(base) == ""      # budget ran out
    # ...the full map still falls back to the quick regex map
    out = rm._build_repo_map(base)
    assert "a.py" in out and "main" in out
    assert len(calls) == 1                            # one parse, not two


def test_the_next_turn_uses_the_parse_that_kept_running(slow_digest):
    gate, calls, base = slow_digest
    assert rm._aider_digest_bounded(base) == ""
    gate.set()
    assert rm._aider_digest_bounded(base) == "a.py: main"
    assert len(calls) == 1


def test_warming_at_session_open_is_what_the_turn_reads(slow_digest):
    gate, calls, base = slow_digest
    rm.warm_repo_map(base)
    rm.warm_repo_map(base)                            # idempotent while running
    gate.set()
    assert rm._aider_digest_bounded(base) == "a.py: main"
    assert len(calls) == 1


def test_warming_honours_the_map_switch(slow_digest, monkeypatch):
    gate, calls, base = slow_digest
    monkeypatch.setenv("AIFORGE_CHAT_AIDER_MAP", "0")
    rm.warm_repo_map(base)
    assert calls == [] and rm._WARM == {}


def test_warming_never_raises(monkeypatch):
    monkeypatch.setattr(rm, "_workspace_root",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    rm.warm_repo_map("/nope")


def test_the_default_wait_is_short(monkeypatch):
    monkeypatch.delenv("AIFORGE_REPOMAP_BUDGET_S", raising=False)
    assert rm._digest_budget_s() == 3.0
