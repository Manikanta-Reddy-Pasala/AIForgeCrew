import json

import pytest

from aiforge_core.runtime import chat_agent as ca


def _scripted(outputs):
    """Return a complete_fn that yields the given outputs in order."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)
    return _fn


def _collect(gen):
    return list(gen)


def test_final_immediately(tmp_path):
    fn = _scripted(["THOUGHT: easy\nFINAL: hello there"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    assert evs[-1] == {"type": "done"}
    msg = [e for e in evs if e["type"] == "message"][0]
    assert msg["text"] == "hello there"


def test_loop_detection_same_action(tmp_path):
    # Always the SAME action — instead of circling, the agent ASKS the user
    # (message with awaiting_input) and stops.
    def _fn(role, messages, **kw):
        return 'ACTION: list_dir\nARGS_JSON: {"path": "."}'
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "loop"}], cwd=str(tmp_path),
        complete_fn=_fn))
    asks = [e for e in evs if e["type"] == "message" and e.get("awaiting_input")]
    assert asks and ("clarify" in asks[0]["text"] or "proceed" in asks[0]["text"])
    # Bounded well below the runaway cap: the stuck guard now RECOVERS
    # (recap + nudge, clearing the strike count) a few times before it gives up,
    # so the ceiling is _LOOP_REPEAT strikes per recovery round — not one round.
    from aiforge_core.runtime.chat_agent._context import _stuck_recovery_max
    assert len([e for e in evs if e["type"] == "tool"]) <= (
        ca._LOOP_REPEAT * (_stuck_recovery_max() + 1))
    assert evs[-1] == {"type": "done"}


def test_agent_can_ask_a_question(tmp_path):
    def _fn(role, messages, **kw):
        return "THOUGHT: need detail\nASK: Which port should I use?"
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "run it"}], cwd=str(tmp_path),
        complete_fn=_fn))
    msg = [e for e in evs if e["type"] == "message"]
    assert msg and msg[0].get("awaiting_input") is True
    assert "Which port" in msg[0]["text"]


def test_progressing_actions_not_killed(tmp_path):
    # Different actions each step → NOT a loop; runs until FINAL.
    seq = [f'ACTION: list_dir\nARGS_JSON: {{"path": "{i}"}}' for i in range(6)]
    seq.append("FINAL: done")
    fn = _scripted(seq)
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "work"}], cwd=str(tmp_path), complete_fn=fn))
    # no loop error; finished normally
    assert not [e for e in evs if e["type"] == "error"]
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "done"


def test_llm_error_is_soft(tmp_path, monkeypatch):
    # Keep the retry backoff from actually sleeping (default is now 3 retries
    # with escalating 3s/6s/9s waits) so the test stays fast.
    monkeypatch.setenv("AIFORGE_CHAT_LLM_RETRIES", "1")
    monkeypatch.setattr(ca.time, "sleep", lambda *_a, **_k: None)

    def _fn(role, messages, **kw):
        raise RuntimeError("boom")
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=_fn))
    # A transient LLM failure is handled SOFTLY: a plain ⚠️ message (never a raw
    # "error" / llm.exhausted stack), then a clean done — nothing was changed.
    assert any(e["type"] == "message" and "didn't respond" in e.get("text", "")
               for e in evs)
    assert not any(e["type"] == "error" for e in evs)
    assert evs[-1] == {"type": "done"}
