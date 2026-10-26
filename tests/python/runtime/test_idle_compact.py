"""A chat idle for ``idle_compact_min`` gets its carried context compacted —
never while any run is in flight, once per saved transcript, one chat a
pass, and only when the transcript is big enough to matter."""
from __future__ import annotations

import json
import os
import time

import pytest

from aiforge_core.runtime import chat_compact, chat_runs, chat_store, idle_compact


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_IDLE_COMPACT_MIN", "20")
    monkeypatch.setenv("AIFORGE_IDLE_COMPACT_MIN_TOKENS", "1000")
    monkeypatch.setattr(chat_runs, "any_active", lambda: False)
    monkeypatch.setattr(chat_runs, "last_activity", lambda: 0.0)
    monkeypatch.setattr(chat_store, "get_session",
                        lambda sid: {"id": sid, "role": "chat"})
    calls = []

    def fake_compact(sid, role="chat"):
        calls.append(sid)
        path = tmp_path / "chat_transcripts" / f"session_{sid}.json"
        data = json.loads(path.read_text())
        data["messages"] = data["messages"][-1:]
        path.write_text(json.dumps(data))
        return {"ok": True, "compacted": True, "before": 1, "after": 0}
    monkeypatch.setattr(chat_compact, "compact_saved", fake_compact)
    idle_compact._reset_for_tests()
    yield tmp_path, calls
    idle_compact._reset_for_tests()


def _transcript(root, sid, chars, idle_min):
    d = root / "chat_transcripts"
    d.mkdir(exist_ok=True)
    p = d / f"session_{sid}.json"
    p.write_text(json.dumps({"users": 1, "mark": "m", "cpt": 4.0, "messages": [
        {"role": "user", "content": "x" * chars},
        {"role": "assistant", "content": "ok"}]}))
    t = time.time() - idle_min * 60
    os.utime(p, (t, t))
    return p


def test_an_idle_big_chat_is_compacted_once(env):
    root, calls = env
    p = _transcript(root, 7, 40_000, idle_min=30)
    assert idle_compact.sweep()["session"] == 7
    assert calls == [7]
    t = time.time() - 30 * 60                   # idle again, same transcript
    os.utime(p, (t, t))
    assert idle_compact.sweep() == {}           # not again for the same transcript
    assert calls == [7]


def test_a_recent_chat_is_left_alone(env):
    root, calls = env
    _transcript(root, 8, 40_000, idle_min=5)
    assert idle_compact.sweep() == {} and calls == []


def test_a_small_chat_is_not_worth_it(env):
    root, calls = env
    _transcript(root, 9, 400, idle_min=60)
    assert idle_compact.sweep() == {} and calls == []


def test_nothing_while_a_run_is_in_flight(env, monkeypatch):
    root, calls = env
    _transcript(root, 10, 40_000, idle_min=60)
    monkeypatch.setattr(chat_runs, "any_active", lambda: True)
    assert idle_compact.sweep() == {"skipped": "busy"} and calls == []
    monkeypatch.setattr(chat_runs, "any_active", lambda: False)
    monkeypatch.setattr(chat_runs, "last_activity", lambda: time.time() - 10)
    assert idle_compact.sweep() == {"skipped": "busy"} and calls == []


def test_one_chat_per_pass_oldest_first(env):
    root, calls = env
    _transcript(root, 11, 40_000, idle_min=40)
    _transcript(root, 12, 40_000, idle_min=90)
    idle_compact.sweep()
    assert calls == [12]
    idle_compact.sweep()
    assert calls == [12, 11]


def test_zero_minutes_turns_it_off(env, monkeypatch):
    root, calls = env
    monkeypatch.setenv("AIFORGE_IDLE_COMPACT_MIN", "0")
    _transcript(root, 13, 40_000, idle_min=600)
    assert idle_compact.sweep() == {} and calls == []


def test_a_new_turn_makes_it_eligible_again(env):
    root, calls = env
    p = _transcript(root, 14, 40_000, idle_min=30)
    idle_compact.sweep()
    data = json.loads(p.read_text())           # a new turn saved a new transcript
    data["users"] = 2
    data["messages"] = [{"role": "user", "content": "y" * 40_000}]
    p.write_text(json.dumps(data))
    t = time.time() - 30 * 60
    os.utime(p, (t, t))
    idle_compact.sweep()
    assert calls == [14, 14]


def test_a_chat_starting_a_message_is_skipped(env):
    root, calls = env
    _transcript(root, 15, 40_000, idle_min=60)
    lock = chat_runs.start_lock(15)
    with lock:
        assert idle_compact.sweep() == {} and calls == []
    idle_compact._reset_for_tests()
    idle_compact.sweep()
    assert calls == [15]
