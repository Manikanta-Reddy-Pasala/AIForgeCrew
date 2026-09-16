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
    assert _native._queued_steps(msg) == (
        ['ACTION: file_read\nARGS_JSON: {"path": "b.py"}',
         'ACTION: grep\nARGS_JSON: {"pattern": "TODO"}'], 0)


@pytest.mark.parametrize("other", [
    _call("file_write", path="a.py", content="x"),
    _call("run_command", cmd="ls"),
    _call("gitlab_pipeline_watch", project="p", pipeline_id=1),
    _call("web_crawl", url="https://example.com"),
    _call("typecheck", path="."),
])
def test_a_batch_with_a_write_or_slow_tool_runs_one_call(other):
    """A write depends on what the model saw first; a slow read would run far
    past the turn deadline. The model is told the rest did not run."""
    msg = _reply(_call("file_read", path="a.py"), other)
    assert _native._queued_steps(msg) == ([], 1)
    assert _native._queued_steps(_reply(other, _call("file_read", path="a.py"))) \
        == ([], 1)


def test_every_batchable_tool_is_read_only():
    from aiforge_core.runtime.chat_agent._registry import _READONLY_TOOLS
    assert _native.BATCHABLE_READS <= set(_READONLY_TOOLS)


def test_duplicates_are_dropped_and_broken_calls_counted():
    msg = _reply(_call("file_read", path="a.py"), _call("file_read", path="a.py"),
                 {"function": {"name": "grep", "arguments": "{broken"}},
                 _call("list_dir", path="."), _call("list_dir", path="."))
    assert _native._queued_steps(msg) == (
        ['ACTION: list_dir\nARGS_JSON: {"path": "."}'], 1)


def test_the_cap_counts_the_whole_reply(monkeypatch):
    msg = _reply(*[_call("file_read", path=f"{i}.py") for i in range(20)])
    steps, skipped = _native._queued_steps(msg)
    assert (len(steps), skipped) == (7, 12)       # 1 + 7 = 8 per reply
    monkeypatch.setenv("AIFORGE_CHAT_BATCH_READS", "2")
    assert len(_native._queued_steps(msg)[0]) == 1
    monkeypatch.setenv("AIFORGE_CHAT_BATCH_READS", "0")
    assert _native._queued_steps(msg) == ([], 19)


def test_a_single_call_queues_nothing():
    assert _native._queued_steps(_reply(_call("file_read", path="a.py"))) == ([], 0)
    assert _native._queued_steps({"content": "done"}) == ([], 0)


def _native_fn(monkeypatch, *replies):
    from aiforge_core.llm import client
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-batch")
    seq = list(replies)
    monkeypatch.setattr(client, "complete_raw", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(client, "complete", lambda role, convo: "TEXT")
    return _native.make_native_complete_fn()


def test_native_fn_hands_over_the_batch_once(monkeypatch):
    fn = _native_fn(monkeypatch, _reply(
        _call("file_read", path="a.py"), _call("file_read", path="b.py")))
    assert fn("chat", []).startswith("ACTION: file_read")
    assert fn.take_queued() == (['ACTION: file_read\nARGS_JSON: {"path": "b.py"}'], 0)
    assert fn.take_queued() == ([], 0)


def test_a_new_model_call_forgets_an_untaken_batch(monkeypatch):
    two = _reply(_call("file_read", path="a.py"), _call("file_read", path="b.py"))
    fn = _native_fn(monkeypatch, two, {"content": "FINAL: done"})
    fn("chat", [])
    assert fn("chat", []) == "FINAL: done"
    assert fn.take_queued() == ([], 0)


def test_a_text_fallback_turn_queues_nothing(monkeypatch):
    broken = _reply({"function": {"name": "file_read", "arguments": "{bad"}},
                    _call("file_read", path="b.py"))
    fn = _native_fn(monkeypatch, broken)
    assert fn("chat", []) == "TEXT"
    assert fn.take_queued() == ([], 0)


@pytest.fixture
def _two_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    (tmp_path / "c.txt").write_text("gamma")
    return tmp_path


def _batching_fn(batch, final, skipped=0):
    """A fake native model: first reply asks for the whole batch, then answers."""
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
        return items, skipped

    _fn.take_queued = take_queued
    return _fn, calls


def _reads(*names):
    return [f'ACTION: file_read\nARGS_JSON: {{"path": "{n}.txt"}}' for n in names]


def _run(tmp, fn, **kw):
    return list(ca.run_chat_agent(
        [{"role": "user", "content": "read a, b and c"}], cwd=str(tmp),
        complete_fn=fn, **kw))


def _paths(evs):
    return [e["args"]["path"] for e in evs
            if e["type"] == "tool" and e["name"] == "file_read"]


def _seen(calls, i):
    return "\n".join(str(m.get("content")) for m in calls[i])


def test_the_loop_runs_the_whole_batch_before_asking_again(_two_files):
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: read all three")
    evs = _run(_two_files, fn)
    assert _paths(evs) == ["a.txt", "b.txt", "c.txt"]
    assert len(calls) == 2, "three reads cost one model call, not three"
    for text in ("alpha", "beta", "gamma"):
        assert text in _seen(calls, 1)
    assert "tool calls in your last reply" not in _seen(calls, 1)
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "read all three"


def test_the_model_is_told_about_calls_that_did_not_run(_two_files):
    fn, calls = _batching_fn(_reads("a"), "FINAL: ok", skipped=2)
    _run(_two_files, fn)
    assert "NOTE: 2 of the tool calls in your last reply did not run" \
        in _seen(calls, 1)


def test_a_steer_drops_the_rest_of_the_batch(_two_files, monkeypatch):
    from aiforge_core.runtime import chat_interject
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: ok")
    monkeypatch.setattr(chat_interject, "pending", lambda sid: True)
    monkeypatch.setattr(chat_interject, "drain_items", lambda sid: [])
    evs = _run(_two_files, fn, session_id=987654)
    assert _paths(evs) == ["a.txt"]
    assert len(calls) == 2
    assert "2 of the tool calls" in _seen(calls, 1)


def test_stop_drops_the_rest_of_the_batch(_two_files, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: ok")
    ran = []
    real = chat_cancel.is_cancelled
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: bool(ran) or real(sid))
    _orig = fn.take_queued

    def _take():
        ran.append(1)
        return _orig()
    fn.take_queued = _take
    evs = _run(_two_files, fn, session_id=987655)
    assert _paths(evs) == ["a.txt"]
    assert len(calls) == 1
    assert any(e["type"] == "error" and "stopped" in e["text"] for e in evs)


def test_quick_mode_keeps_a_step_for_the_answer(_two_files):
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: two were enough")
    evs = _run(_two_files, fn, max_steps=3)
    assert _paths(evs) == ["a.txt", "b.txt"]
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "two were enough"
    assert "step budget is nearly used up" in _seen(calls, 1)


def test_a_batch_stops_before_it_outgrows_the_context(_two_files, monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setattr(_loop, "_tail_budget_chars", lambda *a, **k: 5)
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: ok")
    evs = _run(_two_files, fn)
    assert _paths(evs) == ["a.txt"]
    assert "would not fit in the context window" in _seen(calls, 1)


def test_a_refused_call_drops_the_rest_of_the_batch(_two_files, monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "list_dir=deny")
    batch = [_reads("a")[0], 'ACTION: list_dir\nARGS_JSON: {"path": "."}',
             _reads("c")[0]]
    fn, calls = _batching_fn(batch, "FINAL: ok")
    evs = _run(_two_files, fn)
    assert _paths(evs) == ["a.txt"]
    assert "an earlier call was blocked" in _seen(calls, 1)


def test_a_repeated_read_in_a_batch_does_not_drop_the_rest(_two_files):
    """The duplicate-read guard only skips that call."""
    replies = iter([_reads("a")[0], None, "FINAL: ok"])
    calls, queued = [], []

    def fn(role, convo):
        calls.append(list(convo))
        out = next(replies)
        if out is None:
            queued[:] = _reads("b", "c")
            return _reads("a")[0]
        return out

    def take():
        items = list(queued)
        queued.clear()
        return items, 0
    fn.take_queued = take
    evs = _run(_two_files, fn)
    assert _paths(evs) == ["a.txt", "b.txt", "c.txt"]
    assert len(calls) == 3
    assert "tool calls in your last reply" not in _seen(calls, 2)


def test_the_deadline_stops_a_batch():
    import time
    from types import SimpleNamespace

    from aiforge_core.runtime.chat_agent import _loop
    st = SimpleNamespace(capped=False, safety=0, turn_deadline=time.monotonic() - 1,
                         convo=[], batch_mark=0, role="chat")
    assert _loop._batch_stop_reason(st, 1, None) == "the turn deadline passed"
    st.turn_deadline = None
    assert _loop._batch_stop_reason(st, 1, None) is None


def test_a_condense_keeps_results_the_model_has_not_read(monkeypatch):
    from aiforge_core.runtime.chat_agent._context import _compaction
    monkeypatch.setattr(_compaction, "_ctx_budget_chars", lambda *a, **k: 1000)
    convo = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"{i} " + "x" * 300}
        for i in range(30)]
    kept = _compaction._compact_convo(convo, role="chat", keep_min=12)
    assert kept[-12:] == convo[-12:]
    assert len(kept) == 13
    assert len(_compaction._compact_convo(convo, role="chat")) < 13


def test_only_native_runs_are_told_to_batch(tmp_path, monkeypatch):
    msgs = [{"role": "user", "content": "hi"}]
    kw = dict(readonly_mode=False, plan_mode=False, analyze_mode=False,
              builder=None, strict_finish=False, session_id=None)
    text, *_ = _build_convo(msgs, str(tmp_path), "chat", **kw)
    native, *_ = _build_convo(msgs, str(tmp_path), "chat", native=True, **kw)
    assert "BATCH READS" not in text[0]["content"]
    assert "BATCH READS" in native[0]["content"]
    monkeypatch.setenv("AIFORGE_CHAT_BATCH_READS", "1")
    off, *_ = _build_convo(msgs, str(tmp_path), "chat", native=True, **kw)
    assert "BATCH READS" not in off[0]["content"]


def test_the_prompt_forbids_promises_and_made_up_results():
    from aiforge_core.runtime.chat_agent._prompt import _SYSTEM
    for rule in ("NEVER END ON A PROMISE", "NEVER FABRICATE", "BE OBJECTIVE"):
        assert rule in _SYSTEM
