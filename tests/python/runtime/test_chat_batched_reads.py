"""A reply that asks for several reads runs them all before the next model call.

The native adapter used to keep only the first tool call, so a model that asked
for three files paid three extra round trips — and often forgot the others.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent import _native
from aiforge_core.runtime.chat_agent._loop import _build_convo


def _call(name, **args):
    return {"type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def test_all_read_calls_after_the_first_are_queued():
    msg = _reply(_call("file_read", path="a.py"), _call("file_read", path="b.py"),
                 _call("grep", pattern="TODO"))
    assert _native._synth_step(msg).endswith('{"path": "a.py"}')
    queued = _native._queued_steps(msg)
    assert queued == ['ACTION: file_read\nARGS_JSON: {"path": "b.py"}',
                      'ACTION: grep\nARGS_JSON: {"pattern": "TODO"}']


def test_a_batch_with_a_write_keeps_one_call_per_turn():
    """A write depends on what the model saw first; it must ask again."""
    msg = _reply(_call("file_read", path="a.py"),
                 _call("file_write", path="a.py", content="x"))
    assert _native._queued_steps(msg) == []
    msg = _reply(_call("file_read", path="a.py"), _call("run_command", cmd="ls"))
    assert _native._queued_steps(msg) == []


def test_duplicates_and_broken_calls_are_dropped():
    msg = _reply(_call("file_read", path="a.py"), _call("file_read", path="a.py"),
                 {"function": {"name": "grep", "arguments": "{broken"}},
                 _call("list_dir", path="."), _call("list_dir", path="."))
    assert _native._queued_steps(msg) == ['ACTION: list_dir\nARGS_JSON: {"path": "."}']


def test_the_queue_is_capped(monkeypatch):
    msg = _reply(*[_call("file_read", path=f"{i}.py") for i in range(20)])
    assert len(_native._queued_steps(msg)) == 8
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "2")
    assert len(_native._queued_steps(msg)) == 2
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "0")
    assert _native._queued_steps(msg) == []


def test_a_single_call_queues_nothing():
    assert _native._queued_steps(_reply(_call("file_read", path="a.py"))) == []
    assert _native._queued_steps({"content": "done"}) == []


def test_native_fn_hands_over_the_batch_once(monkeypatch):
    from aiforge_core.llm import client
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-batch")
    monkeypatch.setattr(client, "complete_raw", lambda *a, **k: _reply(
        _call("file_read", path="a.py"), _call("file_read", path="b.py")))
    fn = _native.make_native_complete_fn()
    assert fn("chat", []).startswith("ACTION: file_read")
    assert fn.take_queued() == ['ACTION: file_read\nARGS_JSON: {"path": "b.py"}']
    assert fn.take_queued() == []


@pytest.fixture
def _two_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    (tmp_path / "c.txt").write_text("gamma")
    return tmp_path


def _batching_fn(batch, final):
    """A fake native model: first reply asks for every file, second answers."""
    calls = []
    queued = []

    def _fn(role, convo):
        calls.append(list(convo))
        queued.clear()
        if len(calls) == 1:
            queued.extend(batch[1:])
            return batch[0]
        return final

    def take_queued():
        items = list(queued)
        queued.clear()
        return items

    _fn.take_queued = take_queued
    return _fn, calls


def test_the_loop_runs_the_whole_batch_before_asking_again(_two_files):
    steps = [f'ACTION: file_read\nARGS_JSON: {{"path": "{n}.txt"}}' for n in "abc"]
    fn, calls = _batching_fn(steps, "FINAL: read all three")
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "read a, b and c"}],
        cwd=str(_two_files), complete_fn=fn))
    reads = [e for e in evs if e["type"] == "tool" and e["name"] == "file_read"]
    assert [e["args"]["path"] for e in reads] == ["a.txt", "b.txt", "c.txt"]
    assert len(calls) == 2, "three reads cost one model call, not three"
    seen = "\n".join(str(m.get("content")) for m in calls[1])
    for text in ("alpha", "beta", "gamma"):
        assert text in seen
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "read all three"


def test_a_steer_drops_the_rest_of_the_batch(_two_files, monkeypatch):
    from aiforge_core.runtime import chat_interject
    steps = [f'ACTION: file_read\nARGS_JSON: {{"path": "{n}.txt"}}' for n in "abc"]
    fn, calls = _batching_fn(steps, "FINAL: ok")
    monkeypatch.setattr(chat_interject, "pending", lambda sid: True)
    monkeypatch.setattr(chat_interject, "drain_items", lambda sid: [])
    evs = list(ca.run_chat_agent(
        [{"role": "user", "content": "read"}], cwd=str(_two_files),
        complete_fn=fn, session_id=987654))
    reads = [e for e in evs if e["type"] == "tool" and e["name"] == "file_read"]
    assert [e["args"]["path"] for e in reads] == ["a.txt"]
    assert len(calls) == 2


def test_only_native_runs_are_told_to_batch(tmp_path):
    msgs = [{"role": "user", "content": "hi"}]
    kw = dict(readonly_mode=False, plan_mode=False, analyze_mode=False,
              builder=None, strict_finish=False, session_id=None)
    text, *_ = _build_convo(msgs, str(tmp_path), "chat", **kw)
    native, *_ = _build_convo(msgs, str(tmp_path), "chat", native=True, **kw)
    assert "BATCH READS" not in text[0]["content"]
    assert "BATCH READS" in native[0]["content"]


def test_the_prompt_forbids_promises_and_made_up_results():
    from aiforge_core.runtime.chat_agent._prompt import _SYSTEM
    for rule in ("NEVER END ON A PROMISE", "NEVER FABRICATE", "BE OBJECTIVE"):
        assert rule in _SYSTEM
