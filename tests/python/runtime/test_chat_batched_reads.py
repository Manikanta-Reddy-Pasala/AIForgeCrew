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
    """A write's arguments were chosen before the reads returned, and a slow
    tool would run far past the turn deadline. That call is held. A read
    beside it still runs, so the model does not pay another round trip to
    see the file."""
    msg = _reply(_call("file_read", path="a.py"), other)
    assert _native._queued_steps(msg) == ([], 1)
    steps, skipped = _native._queued_steps(
        _reply(other, _call("file_read", path="a.py")))
    assert steps == ['ACTION: file_read\nARGS_JSON: {"path": "a.py"}']
    assert skipped == 0


def test_reads_in_a_mixed_reply_run_and_the_write_is_held():
    msg = _reply(_call("file_read", path="a.py"),
                 _call("grep", pattern="TODO"),
                 _call("file_patch", path="a.py", old_text="a", new_text="b"))
    steps, skipped = _native._queued_steps(msg)
    assert steps == ['ACTION: grep\nARGS_JSON: {"pattern": "TODO"}']
    assert skipped == 1


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
    assert "tool calls in your last reply did not run: 2 because only " \
        "quick read-only calls run together" in _seen(calls, 1)


def test_a_steer_drops_the_rest_of_the_batch(_two_files, monkeypatch):
    """A message that arrives while the reply is in hand: do not start the
    tool that reply chose, and do not run the reads batched behind it."""
    from aiforge_core.runtime import chat_interject
    fn, calls = _batching_fn(_reads("a", "b", "c"), "FINAL: ok")
    waiting = {"on": False}

    def _fn(role, convo):
        out = fn(role, convo)
        if len(calls) == 1:
            waiting["on"] = True
        return out

    _fn.take_queued = fn.take_queued

    def _drain(sid):
        waiting["on"] = False
        return []

    monkeypatch.setattr(chat_interject, "pending", lambda sid: waiting["on"])
    monkeypatch.setattr(chat_interject, "drain_items", _drain)
    evs = _run(_two_files, _fn, session_id=987654)
    assert _paths(evs) == []
    assert len(calls) == 2
    assert "2 because the user sent new instructions" in _seen(calls, 1)


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
    from aiforge_core.runtime.chat_agent._turn import _batch
    monkeypatch.setattr(_batch, "_tail_budget_chars", lambda *a, **k: 5)
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
    monkeypatch.setattr(_compaction, "_ctx_budget_chars", lambda *a, **k: 10000)
    convo = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"{i:02} " + "x" * 300}
        for i in range(60)]
    kept = _compaction._compact_convo(convo, role="chat", keep_min=25)
    assert kept[-25:] == convo[-25:]
    assert len(kept) == 26
    assert len(_compaction._compact_convo(convo, role="chat")) < 26


def test_unread_results_too_big_for_the_window_are_not_forced_in(monkeypatch):
    """Keeping them would only make the model call fail."""
    from aiforge_core.runtime.chat_agent._context import _compaction
    monkeypatch.setattr(_compaction, "_ctx_budget_chars", lambda *a, **k: 1000)
    convo = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 else "assistant", "content": "x" * 300}
        for i in range(30)]
    assert len(_compaction._compact_convo(convo, role="chat", keep_min=12)) < 13


def test_the_note_names_each_reason():
    from types import SimpleNamespace

    from aiforge_core.runtime.chat_agent import _loop
    st = SimpleNamespace(pending_steps=["a", "b"], batch_skipped=3, early_reads={},
                         convo=[{"role": "user", "content": "OBSERVATION: x"}])
    _loop._drop_batch(st, "an earlier call was blocked")
    note = st.convo[-1]["content"]
    assert "2 because an earlier call was blocked" in note
    assert "3 because only quick read-only calls" in note
    assert st.pending_steps == [] and st.batch_skipped == 0
    _loop._drop_batch(st, "an earlier call was blocked")
    assert st.convo[-1]["content"] == note          # nothing new to report


def test_the_mark_follows_the_results_through_a_condense():
    from types import SimpleNamespace

    from aiforge_core.runtime.chat_agent import _loop
    st = SimpleNamespace(batch_unread=True, batch_mark=40, convo=[{}] * 10)
    _loop._rebase_batch(st, 6)
    assert st.batch_mark == 4 and _loop._unread_batch_msgs(st) == 6


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


# --- slow reads of a batch run at the same time --------------------------

def _jira_reads(*keys):
    return [f'ACTION: jira_read\nARGS_JSON: {{"key": "{k}"}}' for k in keys]


@pytest.fixture
def _slow_jira(monkeypatch):
    """jira_read that takes 0.4 s. Each read is recorded when it STARTS as
    [key, thread, start, end]; end is filled in when it finishes."""
    import threading
    import time

    from aiforge_core.runtime.chat_agent._registry import TOOLS
    ran = []

    def _read(args, cwd):
        row = [args["key"], threading.current_thread().name, time.monotonic(), None]
        ran.append(row)
        time.sleep(0.4)
        row[3] = time.monotonic()
        return {"ok": True, "text": f"issue {args['key']}"}
    monkeypatch.setitem(TOOLS, "jira_read", _read)
    return ran


def _keys(evs):
    return [e["args"]["key"] for e in evs
            if e["type"] == "tool" and e["name"] == "jira_read"]


def _early(ran):
    return sorted(r[0] for r in ran if r[1].startswith("batch-read"))


def _span(ran, key):
    return next((r[2], r[3]) for r in ran if r[0] == key)


def test_slow_reads_of_a_batch_run_at_the_same_time(tmp_path, _slow_jira):
    fn, calls = _batching_fn(_jira_reads("A-1", "A-2", "A-3", "A-4"), "FINAL: ok")
    evs = _run(tmp_path, fn)
    assert _keys(evs) == ["A-1", "A-2", "A-3", "A-4"], "results keep reply order"
    assert _early(_slow_jira) == ["A-2", "A-3", "A-4"]
    spans = [_span(_slow_jira, k) for k in ("A-1", "A-2", "A-3", "A-4")]
    assert max(s for s, _ in spans) < min(e for _, e in spans), \
        "every read started before the first one ended"
    assert len(calls) == 2
    for key in ("A-1", "A-2", "A-3", "A-4"):
        assert f"issue {key}" in _seen(calls, 1)


def test_the_parallel_cap_limits_reads_in_flight(tmp_path, _slow_jira, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "1")
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2", "A-3"), "FINAL: ok")
    _run(tmp_path, fn)
    # A-1 runs in the loop alongside one background worker doing A-2 then A-3.
    assert _early(_slow_jira) == ["A-2", "A-3"]
    assert _span(_slow_jira, "A-2")[0] < _span(_slow_jira, "A-1")[1]
    assert _span(_slow_jira, "A-3")[0] >= _span(_slow_jira, "A-2")[1]


def test_cap_zero_runs_the_batch_in_line(tmp_path, _slow_jira, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "0")
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    assert _keys(_run(tmp_path, fn)) == ["A-1", "A-2"]
    assert _early(_slow_jira) == []


def test_a_read_a_hook_watches_is_not_started_early(tmp_path, _slow_jira,
                                                     monkeypatch):
    """A PreToolUse hook may block the call: it has to see it before it runs."""
    from aiforge_core.runtime import hooks
    monkeypatch.setattr(hooks, "has_matching", lambda event, tool, cwd=None: True)
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    assert _keys(_run(tmp_path, fn)) == ["A-1", "A-2"]
    assert _early(_slow_jira) == []


def test_a_read_that_needs_approval_is_not_started_early(tmp_path, _slow_jira,
                                                          monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "jira_read=ask")
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    _run(tmp_path, fn)
    assert _early(_slow_jira) == []


def test_reads_after_a_lookup_still_overlap(tmp_path, _slow_jira, monkeypatch):
    """memory_lookup is batchable, so the reads beside it start while it
    runs. They do not wait for it to finish, and they still overlap."""
    import time

    from aiforge_core.runtime.chat_agent._registry import TOOLS
    span = {}

    def _lookup(args, cwd):
        span["start"] = time.monotonic()
        time.sleep(0.5)
        span["end"] = time.monotonic()
        return {"ok": True}
    monkeypatch.setitem(TOOLS, "memory_lookup", _lookup)
    batch = ['ACTION: memory_lookup\nARGS_JSON: {"query": "auth"}',
             *_jira_reads("A-1", "A-2")]
    fn, _ = _batching_fn(batch, "FINAL: ok")
    evs = _run(tmp_path, fn)
    assert _keys(evs) == ["A-1", "A-2"]
    assert _early(_slow_jira) == ["A-1", "A-2"]
    assert _span(_slow_jira, "A-1")[0] < span["end"]
    assert _span(_slow_jira, "A-2")[0] < span["end"]


def test_early_reads_start_for_every_batchable_first_call():
    from aiforge_core.runtime.chat_agent._turn._batch import _first_runs_alongside
    started = (
        'ACTION: file_read\nARGS_JSON: {"path": "a.py"}',
        'ACTION: memory_lookup\nARGS_JSON: {"query": "auth"}',
        'ACTION: skill_search\nARGS_JSON: {"query": "auth"}',
        'ACTION: resolve_repo\nARGS_JSON: {"name": "crew"}',
        'ACTION: plan_progress\nARGS_JSON: {"slug": "tests", "title": "Tests"}',
    )
    held = (
        'ACTION: file_patch\nARGS_JSON: {"path": "a.py", "old_text": "a", "new_text": "b"}',
        'ACTION: file_write\nARGS_JSON: {"path": "a.py", "content": "x"}',
        'ACTION: editor\nARGS_JSON: {"command": "str_replace", "path": "a.py"}',
        'ACTION: run_command\nARGS_JSON: {"cmd": "ls"}',
    )
    assert all(_first_runs_alongside(text) for text in started)
    assert not any(_first_runs_alongside(text) for text in held)


def test_a_leading_write_does_not_start_the_reads_beside_it(
        tmp_path, _slow_jira, monkeypatch):
    """file_patch as the first call runs this turn. The reads after it wait
    until that write has finished."""
    import time

    from aiforge_core.runtime.chat_agent._registry import TOOLS
    span = {}

    def _patch(args, cwd):
        span["end"] = None
        time.sleep(0.4)
        span["end"] = time.monotonic()
        return {"ok": True}
    monkeypatch.setitem(TOOLS, "file_patch", _patch)
    batch = ['ACTION: file_patch\nARGS_JSON: {"path": "a.py", "old_text": "a", "new_text": "b"}',
             *_jira_reads("A-1", "A-2")]
    fn, _ = _batching_fn(batch, "FINAL: ok")
    evs = _run(tmp_path, fn)
    assert _keys(evs) == ["A-1", "A-2"]
    assert _span(_slow_jira, "A-1")[0] >= span["end"] - 0.05
    assert _span(_slow_jira, "A-2")[0] >= span["end"] - 0.05


def test_the_batch_rule_runs_a_leading_patch():
    from aiforge_core.runtime.chat_agent._prompt_text import BATCH_READS_RULE
    assert "if the first call is file_patch, it runs now" in BATCH_READS_RULE
    assert "later in that reply is held until your next turn" in BATCH_READS_RULE


def test_local_reads_of_one_reply_start_together(_two_files, monkeypatch):
    import threading

    from aiforge_core.runtime.chat_agent._registry import TOOLS
    threads = []
    real = TOOLS["file_read"]

    def _read(args, cwd):
        threads.append(threading.current_thread().name)
        return real(args, cwd)
    monkeypatch.setitem(TOOLS, "file_read", _read)
    fn, _ = _batching_fn(_reads("a", "b", "c"), "FINAL: ok")
    assert _paths(_run(_two_files, fn)) == ["a.txt", "b.txt", "c.txt"]
    assert any(t.startswith("batch-read") for t in threads)


_DENIED = 'ACTION: list_dir\nARGS_JSON: {"path": "."}'


def test_a_dropped_batch_never_shows_its_early_results(tmp_path, _slow_jira,
                                                       monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "list_dir=deny")
    batch = _jira_reads("A-1") + [_DENIED] + _jira_reads("A-2", "A-3")
    fn, calls = _batching_fn(batch, "FINAL: ok")
    evs = _run(tmp_path, fn)
    assert _keys(evs) == ["A-1"]
    assert _early(_slow_jira) == ["A-2", "A-3"], "they did start early"
    assert "issue A-2" not in _seen(calls, 1)
    assert "2 because an earlier call was blocked" in _seen(calls, 1)


def test_a_dropped_batch_cancels_reads_still_waiting(tmp_path, monkeypatch):
    """One worker: A-2 holds it until the batch is dropped, so A-3 is still
    waiting then and must never run."""
    import threading

    from aiforge_core.runtime.chat_agent._registry import TOOLS
    from aiforge_core.runtime.chat_agent._turn import _batch
    dropped, took_a2, ran = threading.Event(), threading.Event(), []

    def _read(args, cwd):
        ran.append(args["key"])
        if args["key"] == "A-1":       # inline: let the worker take A-2 first
            took_a2.wait(5)
        if args["key"] == "A-2":
            took_a2.set()
            dropped.wait(5)
        return {"ok": True}
    monkeypatch.setitem(TOOLS, "jira_read", _read)
    real_cancel = _batch._cancel_early_reads

    def _cancel(st):
        waiting = bool(st.early_reads)
        real_cancel(st)
        if waiting:
            dropped.set()
    monkeypatch.setattr(_batch, "_cancel_early_reads", _cancel)
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "1")
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "list_dir=deny")
    batch = _jira_reads("A-1") + [_DENIED] + _jira_reads("A-2", "A-3")
    fn, _ = _batching_fn(batch, "FINAL: ok")
    _run(tmp_path, fn)
    assert dropped.is_set()
    threading.Event().wait(0.3)      # time for A-3 to run, had it not been cancelled
    assert sorted(ran) == ["A-1", "A-2"]


def test_the_check_is_per_call(tmp_path, _slow_jira, monkeypatch):
    """A hook on jira_read holds back only jira_read."""
    from aiforge_core.runtime import hooks
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    monkeypatch.setitem(TOOLS, "confluence_read", lambda args, cwd: {"ok": True})
    monkeypatch.setattr(hooks, "has_matching",
                        lambda event, tool, cwd=None: tool == "jira_read")
    started = []
    from aiforge_core.runtime.chat_agent._turn import _batch
    orig = _batch._invoke_tool
    monkeypatch.setattr(_batch, "_invoke_tool",
                        lambda fn, name, args, cwd: started.append(name)
                        or orig(fn, name, args, cwd))
    batch = _jira_reads("A-1", "A-2") + [
        'ACTION: confluence_read\nARGS_JSON: {"page_id": "7"}']
    fn, _ = _batching_fn(batch, "FINAL: ok")
    _run(tmp_path, fn)
    assert started == ["confluence_read"]
    assert _early(_slow_jira) == []


def test_an_early_read_that_raises_is_an_error_result(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._registry import TOOLS

    def _boom(args, cwd):
        raise RuntimeError("jira is down")
    monkeypatch.setitem(TOOLS, "jira_read", _boom)
    fn, calls = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    evs = _run(tmp_path, fn)
    results = [e["result"] for e in evs
               if e["type"] == "tool" and e["name"] == "jira_read"]
    assert results == [{"ok": False, "error": "jira is down"}] * 2
    assert len(calls) == 2


def test_every_concurrent_read_is_batchable_and_read_only():
    """Plan/Analyze mode lets read-only tools through: a concurrent read that
    is not one would be started early and then refused."""
    from aiforge_core.runtime.chat_agent._registry import _READONLY_TOOLS, TOOLS
    assert _native.CONCURRENT_READS <= _native.BATCHABLE_READS
    assert _native.CONCURRENT_READS.issubset(_READONLY_TOOLS)
    assert _native.CONCURRENT_READS.issubset(TOOLS)


@pytest.mark.parametrize("name", [
    "web_crawl",               # writes a work/web dossier
    "email_read",              # an IMAP login per call
    "gitlab_pipeline_watch",   # blocks for minutes
    "context_gather",          # fans out itself and writes its cache
    "repo_map",                # shares aider's map cache
])
def test_tools_with_side_effects_or_long_waits_never_run_concurrently(name):
    assert name not in _native.CONCURRENT_READS


def test_a_web_fetch_the_egress_gate_refuses_is_not_started_early(
        tmp_path, monkeypatch):
    """tool_policy does not look at URLs: the batch asks the egress gate
    itself, so a refused fetch waits for the loop instead of running ahead."""
    from types import SimpleNamespace

    from aiforge_core.net import egress
    from aiforge_core.runtime.chat_agent._turn._batch import (
        _call_sig,
        _can_start_early,
    )
    args = {"url": "https://docs.example.com/page"}
    sig = _call_sig("web_fetch", args)
    st = SimpleNamespace(long_chain_help=False, read_sigs_seen=set(),
                         cwd=str(tmp_path))
    for var in ("AIFORGE_EGRESS_OFF", "AIFORGE_WEB_FETCH_DISABLE",
                "AIFORGE_WEB_SEARCH_DISABLE", "AIFORGE_TOOL_POLICY",
                "AIFORGE_CHAT_TOOL_POLICY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(egress, "host_allowed", lambda url: True)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    assert not _can_start_early(st, "web_fetch", args, sig)
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert _can_start_early(st, "web_fetch", args, sig)
    monkeypatch.setenv("AIFORGE_WEB_FETCH_DISABLE", "1")
    assert not _can_start_early(st, "web_fetch", args, sig)
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE")
    search = {"url": "https://www.google.com/search?q=secret"}
    assert not _can_start_early(st, "web_fetch", search,
                                _call_sig("web_fetch", search))
    monkeypatch.setattr(egress, "host_allowed", lambda url: False)
    assert not _can_start_early(st, "web_fetch", args, sig)


def test_web_fetches_of_a_batch_run_at_the_same_time(tmp_path, monkeypatch):
    import threading
    import time

    from aiforge_core.net import egress
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    for var in ("AIFORGE_EGRESS_OFF", "AIFORGE_WEB_FETCH_DISABLE",
                "AIFORGE_WEB_SEARCH_DISABLE", "AIFORGE_TOOL_POLICY",
                "AIFORGE_CHAT_TOOL_POLICY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(egress, "host_allowed", lambda url: True)
    ran = []

    def _fetch(args, cwd):
        row = [args["url"], threading.current_thread().name,
               time.monotonic(), None]
        ran.append(row)
        time.sleep(0.3)
        row[3] = time.monotonic()
        return {"ok": True, "text": args["url"]}
    monkeypatch.setitem(TOOLS, "web_fetch", _fetch)
    urls = [f"https://docs.example.com/{i}" for i in range(3)]
    batch = [f'ACTION: web_fetch\nARGS_JSON: {{"url": "{u}"}}' for u in urls]
    fn, _ = _batching_fn(batch, "FINAL: ok")
    _run(tmp_path, fn)
    assert sorted(r[0] for r in ran if r[1].startswith("batch-read")) == urls[1:]
    assert max(r[2] for r in ran) < min(r[3] for r in ran), \
        "every fetch started before the first one ended"


def test_a_refused_web_fetch_still_runs_in_order(tmp_path, monkeypatch):
    """Not started early is not dropped: the loop runs it in its turn and the
    model sees the refusal."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    urls = [f"https://docs.example.com/{i}" for i in range(2)]
    batch = [f'ACTION: web_fetch\nARGS_JSON: {{"url": "{u}"}}' for u in urls]
    fn, _ = _batching_fn(batch, "FINAL: ok")
    evs = _run(tmp_path, fn)
    results = [e["result"] for e in evs
               if e["type"] == "tool" and e["name"] == "web_fetch"]
    assert len(results) == 2
    assert all(r["ok"] is False and "web fetch disabled" in r["error"]
               for r in results)


def test_a_turn_that_ends_cancels_reads_still_waiting(tmp_path, monkeypatch):
    """The reply's first call is refused and the run pauses for the user: the
    batch's reads still waiting for a worker must not run after the turn."""
    import threading

    from aiforge_core.runtime.chat_agent import _loop
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    release, took_a2, ran = threading.Event(), threading.Event(), []

    def _read(args, cwd):
        ran.append(args["key"])
        took_a2.set()
        release.wait(5)
        return {"ok": True}
    monkeypatch.setitem(TOOLS, "jira_read", _read)
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "1")

    def _pause(st, step, name, args, sig, n, cwd, session_id):
        took_a2.wait(5)                # the worker holds A-2; A-3 waits
        return "return"
        yield
    monkeypatch.setattr(_loop, "_gated_action", _pause)
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2", "A-3"), "FINAL: ok")
    _run(tmp_path, fn)
    release.set()
    threading.Event().wait(0.3)
    assert ran == ["A-2"], "A-3 was still waiting when the turn ended"


def test_a_read_already_done_is_not_started_again(tmp_path):
    from types import SimpleNamespace

    from aiforge_core.runtime.chat_agent._turn import _batch
    sig = _batch._call_sig("jira_read", {"key": "A-1"})
    st = SimpleNamespace(long_chain_help=True, read_sigs_seen={sig},
                         cwd=str(tmp_path))
    assert not _batch._can_start_early(st, "jira_read", {"key": "A-1"}, sig)
    st.read_sigs_seen = set()
    assert _batch._can_start_early(st, "jira_read", {"key": "A-1"}, sig)


def test_a_batch_that_will_stop_starts_nothing(tmp_path, _slow_jira, monkeypatch):
    import threading

    from aiforge_core.runtime import chat_interject
    monkeypatch.setattr(chat_interject, "pending", lambda sid: True)
    monkeypatch.setattr(chat_interject, "drain_items", lambda sid: [])
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    _run(tmp_path, fn, session_id=987658)
    threading.Event().wait(0.2)      # time for an early read to have started
    assert _early(_slow_jira) == []


def test_a_failure_to_start_runs_the_batch_in_line(tmp_path, _slow_jira,
                                                   monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _batch

    def _no_threads(st):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(_batch, "_start_parallel_reads", _no_threads)
    fn, _ = _batching_fn(_jira_reads("A-1", "A-2"), "FINAL: ok")
    assert _keys(_run(tmp_path, fn)) == ["A-1", "A-2"]
    assert _early(_slow_jira) == []


def test_has_matching_follows_the_hook_files(tmp_path, monkeypatch):
    from aiforge_core.runtime import hooks
    monkeypatch.setattr(hooks, "_global_path", lambda: str(tmp_path / "none.json"))
    (tmp_path / ".aiforge").mkdir()
    (tmp_path / ".aiforge" / "hooks.json").write_text(json.dumps(
        {"PreToolUse": [{"matcher": "jira_read|gitlab_read", "command": "true"}]}))
    assert hooks.has_matching("PreToolUse", "jira_read", str(tmp_path))
    assert not hooks.has_matching("PreToolUse", "confluence_read", str(tmp_path))
    monkeypatch.setenv("AIFORGE_HOOKS_DISABLE", "1")
    assert not hooks.has_matching("PreToolUse", "jira_read", str(tmp_path))
