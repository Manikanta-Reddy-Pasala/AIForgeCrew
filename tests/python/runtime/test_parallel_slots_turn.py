"""Pre-turn context blocks and the next-step prediction overlap only on a model
server with a spare slot. Proven with barriers and events, not wall clocks."""
from __future__ import annotations

import threading

import pytest

from aiforge_core.llm import slots
from aiforge_core.runtime import context_bundle as cb


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for var in ("AIFORGE_LLM_PARALLEL", "AIFORGE_CONTEXT_PARALLEL",
                "AIFORGE_PREDICT_AFTER_DONE_S", "AIFORGE_PREDICT_DISABLE"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    slots.reset()
    yield
    slots.reset()


# ── context bundle ─────────────────────────────────────────────────────────

def _fake_groups(monkeypatch, barrier=None):
    """Replace the four bundle groups with fakes that track concurrency."""
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def _track(field, value):
        def _fill(b, *a, **k):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                if barrier is not None:
                    barrier.wait(timeout=5)
                setattr(b, field, value)
            finally:
                with lock:
                    state["active"] -= 1
        return _fill

    monkeypatch.setattr(cb, "_fill_priority", _track("rules_md", "R"))
    monkeypatch.setattr(cb, "_fill_playbooks", _track("skills_md", "S"))
    monkeypatch.setattr(cb, "_fill_repo_context", _track("repo_map_md", "M"))
    from aiforge_core.runtime import chat_agent as ca
    recall = _track("unused", None)

    def _recall(*_a, **_k):
        holder = type("H", (), {})()
        recall(holder)
        return "RECALL"
    monkeypatch.setattr(ca, "_memory_recall", _recall, raising=False)
    return state


def test_bundle_groups_run_at_once_with_spare_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "2")
    state = _fake_groups(monkeypatch, threading.Barrier(4))
    b = cb.build_bundle(str(tmp_path), "q")    # a barrier timeout would fail
    assert (b.rules_md, b.skills_md, b.repo_map_md, b.memory_md) == \
        ("R", "S", "M", "RECALL")
    assert state["peak"] == 4


def test_bundle_groups_stay_sequential_on_one_slot(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "1")
    state = _fake_groups(monkeypatch)
    b = cb.build_bundle(str(tmp_path), "q")
    assert b.memory_md == "RECALL" and b.repo_map_md == "M"
    assert state["peak"] == 1


def test_bundle_switch_off_is_sequential(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "8")
    monkeypatch.setenv("AIFORGE_CONTEXT_PARALLEL", "0")
    state = _fake_groups(monkeypatch)
    cb.build_bundle(str(tmp_path), "q")
    assert state["peak"] == 1


# ── next-step prediction ───────────────────────────────────────────────────

def _run_turn(tmp_path, on_done=None):
    from aiforge_core.runtime import chat_agent as ca
    out = []
    for ev in ca.run_chat_agent([{"role": "user", "content": "hi"}],
                                cwd=str(tmp_path),
                                complete_fn=lambda _r, _c: "FINAL: hello"):
        out.append(ev)
        if ev.get("type") == "done" and on_done is not None:
            on_done()
    return out


class _Pred:
    def as_event(self):
        return {"type": "suggestion", "text": "run the tests"}


def test_prediction_runs_beside_the_answer_with_spare_slots(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._turn import _finish as F
    from aiforge_core.runtime import next_step
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "2")
    monkeypatch.setattr(next_step, "remember", lambda *a, **k: None)
    release, started = threading.Event(), threading.Event()

    def _predict(*_a, **_k):
        started.set()
        release.wait(5)          # still writing when the answer goes out
        return _Pred()
    monkeypatch.setattr(F, "_predict_next_step", _predict)
    evs = _run_turn(tmp_path, on_done=release.set)
    types_ = [e["type"] for e in evs]
    assert started.is_set()
    assert types_.index("done") < types_.index("suggestion")


def test_prediction_is_skipped_on_one_slot(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._turn import _finish as F
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "1")

    def _boom(*_a, **_k):
        raise AssertionError("no prediction on a one-slot server")
    monkeypatch.setattr(F, "_start_suggestion", _boom)
    evs = _run_turn(tmp_path)
    assert not any(e["type"] == "suggestion" for e in evs)


def test_after_done_wait_ends_on_its_grace():
    from aiforge_core.runtime.chat_agent._turn._suggest_wait import await_ready
    ev = threading.Event()
    assert await_ready(ev, None, grace_s=0.0) is False
    ev.set()
    assert await_ready(ev, None, grace_s=0.0) is True
