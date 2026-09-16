"""The chat agent keeps going on long, multi-task runs.

Four things used to end or derail a run that had been working for hours: the
loop guard counted a test command re-run after every fix as a loop; the
stuck-recovery budget was spent once per run, however much progress came in
between; the model's plan was condensed away with the old messages; and a
model outage of two minutes ended the turn. A run that is really stuck must
still stop.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent._turn import _completion, _limits, _progress, _tasks


def _scripted(outputs):
    seq = list(outputs)
    calls = []

    def _fn(role, convo):
        calls.append(list(convo))
        return seq.pop(0)
    return _fn, calls


def _run(tmp, fn, prompt="do the work", **kw):
    return list(ca.run_chat_agent([{"role": "user", "content": prompt}],
                                  cwd=str(tmp), complete_fn=fn, **kw))


# ── the loop guard ───────────────────────────────────────────────────────

def test_repeating_a_check_after_each_edit_is_not_a_loop(tmp_path):
    (tmp_path / "a.txt").write_text("v0")
    read = 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'
    steps = []
    for i in range(1, 5):
        steps += [read, f'ACTION: file_write\nARGS_JSON: {{"path": "a.txt", '
                        f'"content": "v{i}"}}']
    fn, _ = _scripted([*steps, read, "FINAL: done"])
    evs = _run(tmp_path, fn)
    reads = [e for e in evs if e["type"] == "tool" and e["name"] == "file_read"]
    assert len(reads) == 5
    assert not [e for e in evs if "repeated" in str(e.get("text", ""))]
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "done"


def test_a_real_loop_is_still_caught(tmp_path):
    (tmp_path / "a.txt").write_text("v0")
    cmd = 'ACTION: list_dir\nARGS_JSON: {"path": "."}'
    fn, _ = _scripted([cmd] * 40)
    evs = _run(tmp_path, fn)
    assert any("repeated `list_dir`" in str(e.get("text", "")) for e in evs)
    assert evs[-1]["type"] == "done"


def _loop_state(**kw):
    st = SimpleNamespace(stuck_recoveries=0, action_counts={}, **_progress.progress_fields())
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_progress_refills_the_recovery_budget(tmp_path):
    st = _loop_state()
    assert [_progress.may_recover(st) for _ in range(4)] == [True, True, True, False]
    (tmp_path / "a.py").write_text("new")
    assert _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
    assert _progress.may_recover(st)                 # a new workspace state
    _progress.note_read(st, {"path": "b.py"}, {"ok": True})
    st.stuck_recoveries = 3
    assert _progress.may_recover(st)                 # a newly read file


def test_the_total_recoveries_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MAX_RECOVERIES", "5")
    st = _loop_state()
    spent = 0
    for i in range(20):
        _progress.note_read(st, {"path": f"f{i}.py"}, {"ok": True})
        spent += _progress.may_recover(st)
    assert spent == 5


def test_an_unchanged_or_flipped_file_is_not_a_new_state(tmp_path):
    st = _loop_state()
    f = tmp_path / "a.py"
    states = []
    for content in ("A", "A", "B", "A", "B"):
        f.write_text(content)
        _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
        states.append(st.state_fp)
    assert states[0] == states[1] == states[3]
    assert states[2] == states[4] != states[0]
    assert st.new_states == 2


def test_a_view_is_not_an_edit(tmp_path):
    st = _loop_state()
    assert not _progress.note_write(st, "editor", {"command": "view", "path": "a"},
                                    {"ok": True}, tmp_path)
    assert not _progress.note_write(st, "file_write", {"path": "a"},
                                    {"ok": False}, tmp_path)


def test_rewriting_the_same_file_forever_stops(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    write = 'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "same"}'
    read = 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'
    fn, calls = _scripted([write, read] * 200)
    evs = _run(tmp_path, fn, session_id=None)
    assert evs[-1]["type"] == "done"
    assert len(calls) < 60
    assert any("without progress" in str(e.get("text", "")) for e in evs)


def test_slicing_one_file_forever_stops(tmp_path):
    (tmp_path / "a.txt").write_text("x\n" * 500)
    replies = []
    for i in range(200):
        replies += [f'ACTION: read_lines\nARGS_JSON: {{"path": "a.txt", '
                    f'"start": {i + 1}, "end": {i + 2}}}',
                    'ACTION: list_dir\nARGS_JSON: {"path": "."}']
    fn, calls = _scripted(replies)
    evs = _run(tmp_path, fn)
    assert evs[-1]["type"] == "done"
    assert len(calls) < 150


# ── the task board ───────────────────────────────────────────────────────

def test_the_model_adds_and_updates_its_own_items():
    board = _tasks.seed_board(["fix the bug"])
    res, evs = _tasks.apply_progress(board, {"slug": "docs", "title": "Update docs"})
    assert res["ok"] and board["docs"]["status"] == "pending"
    assert evs[0]["type"] == "subtasks"
    assert [i["slug"] for i in evs[0]["items"]] == ["part-1", "docs"]
    assert res["open"] == ["part-1: fix the bug", "docs: Update docs"]
    res, evs = _tasks.apply_progress(board, {"slug": "docs", "status": "completed"})
    assert board["docs"]["status"] == "done"
    assert res["open_count"] == 1
    assert evs == [{"type": "subtask_update", "slug": "docs", "status": "done"}]
    assert _tasks.open_items(board) == ["part-1"]
    assert _tasks.open_planned(board) == []


def test_bad_progress_calls_are_refused():
    board = {}
    assert not _tasks.apply_progress(board, {})[0]["ok"]
    assert not _tasks.apply_progress(board, {"slug": "x", "status": "nope"})[0]["ok"]
    # an unknown slug without a title is still accepted, as before, and the
    # dock is told about it
    res, evs = _tasks.apply_progress(board, {"slug": "x", "status": "in progress"})
    assert res["ok"] and board["x"]["status"] == "running"
    assert evs[0]["type"] == "subtasks"


def test_the_board_is_pinned_once_and_replaced():
    board = _tasks.seed_board(["a", "b"])
    convo = [{"role": "system", "content": "sys"}]
    _tasks.pin_board(convo, board)
    board["part-1"]["status"] = "done"
    _tasks.pin_board(convo, board)
    text = convo[0]["content"]
    assert text.count(_tasks._BOARD_OPEN) == 1
    assert "[x] part-1: a" in text and "[ ] part-2: b" in text
    assert "(1 of 2 still open" in text


def test_a_condense_pins_the_board_back():
    board = _tasks.seed_board(["a"])
    st = SimpleNamespace(board=board, batch_unread=False,
                         convo=[{"role": "system", "content": "sys"}])
    _limits._after_condense(st, 0, before=5)
    assert _tasks._BOARD_OPEN in st.convo[0]["content"]
    st2 = SimpleNamespace(board=board, batch_unread=False,
                          convo=[{"role": "system", "content": "sys"}])
    _limits._after_condense(st2, 0, before=1)          # nothing was condensed
    assert _tasks._BOARD_OPEN not in st2.convo[0]["content"]


def _plan(slug, **kw):
    import json
    return f"ACTION: plan_progress\nARGS_JSON: {json.dumps({'slug': slug, **kw})}"


def test_final_with_open_planned_items_keeps_going(tmp_path):
    fn, calls = _scripted([
        _plan("one", title="first thing"),
        _plan("two", title="second thing"),
        _plan("one", status="done"),
        "FINAL: did the first thing",
        _plan("two", status="done"),
        "FINAL: did both",
    ])
    evs = _run(tmp_path, fn)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "did both"
    reminder = str(calls[4][-1]["content"])
    assert "two: second thing" in reminder and "one: first thing" not in reminder
    docks = [e for e in evs if e["type"] == "subtasks"]
    assert [i["slug"] for i in docks[-1]["items"]] == ["one", "two"]


def test_the_reminder_is_bounded(tmp_path):
    fn, _ = _scripted([_plan("one", title="never done")] + ["FINAL: gave up"] * 8)
    evs = _run(tmp_path, fn)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "gave up"
    nudges = [e for e in evs if "still open" in str(e.get("text", ""))]
    assert len(nudges) == 2


def test_plan_mode_is_not_pushed_to_do_the_plan(tmp_path):
    fn, _ = _scripted([_plan("one", title="later"), "FINAL: the plan"])
    evs = _run(tmp_path, fn, mode="plan")
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "the plan"


def test_closing_an_item_gives_the_reminders_back(tmp_path):
    fn, _ = _scripted([
        _plan("one", title="first"), _plan("two", title="second"),
        "FINAL: early", "FINAL: early",          # two reminders
        _plan("one", status="done"),
        "FINAL: early again",                    # reminder again: progress came in
        "FINAL: early again", "FINAL: stop here"])
    evs = _run(tmp_path, fn)
    nudges = [e for e in evs if "still open" in str(e.get("text", ""))]
    assert len(nudges) == 4
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "stop here"


def test_the_final_text_is_shown_when_a_reminder_holds_it(tmp_path):
    fn, _ = _scripted([_plan("one", title="first"), "FINAL: half done",
                       _plan("one", status="done"), "FINAL: all done"])
    evs = _run(tmp_path, fn)
    assert any(e["type"] == "thought" and e["text"] == "half done" for e in evs)


def test_progress_and_reads_in_one_reply_all_run(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    batch = [_plan("read", title="read both"),
             'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}',
             'ACTION: file_read\nARGS_JSON: {"path": "b.txt"}']
    calls, queued = [], []

    def fn(role, convo):
        calls.append(list(convo))
        queued.clear()
        if len(calls) == 1:
            queued.extend(batch[1:])
            return batch[0]
        return _plan("read", status="done") if len(calls) == 2 else "FINAL: ok"

    fn.take_queued = lambda: (list(queued), 0)
    evs = _run(tmp_path, fn)
    reads = [e for e in evs if e["type"] == "tool" and e["name"] == "file_read"]
    assert len(reads) == 2
    assert "tool calls in your last reply" not in "\n".join(
        str(m["content"]) for m in calls[1])


def test_the_long_run_rule_only_when_the_run_is_unlimited(tmp_path):
    from aiforge_core.runtime.chat_agent._turn._convo import _build_convo
    kw = dict(readonly_mode=False, plan_mode=False, analyze_mode=False,
              builder=None, strict_finish=False, session_id=None)
    msgs = [{"role": "user", "content": "hi"}]
    on = _build_convo(msgs, str(tmp_path), "chat", unlimited=True, **kw)[0]
    off = _build_convo(msgs, str(tmp_path), "chat", **kw)[0]
    assert "LONG AND MULTI-TASK WORK" in on[0]["content"]
    assert "LONG AND MULTI-TASK WORK" not in off[0]["content"]


def test_the_board_is_a_native_tool_and_batches_with_reads():
    from aiforge_core.runtime.chat_agent import _native
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_NAMES
    assert "plan_progress" in NATIVE_TOOL_NAMES
    msg = {"tool_calls": [
        {"function": {"name": "plan_progress", "arguments": '{"slug": "a"}'}},
        {"function": {"name": "file_read", "arguments": '{"path": "x"}'}}]}
    assert _native._queued_steps(msg)[0] == ['ACTION: file_read\nARGS_JSON: {"path": "x"}']


# ── model outages ────────────────────────────────────────────────────────

def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


@pytest.fixture
def _no_sleep(monkeypatch):
    monkeypatch.setattr(_completion.time, "sleep", lambda s: None)
    monkeypatch.setattr(_completion, "_complete_cancellable",
                        lambda fn, role, convo, sid: fn(role, convo))


def _flaky(fails, exc=None):
    exc = exc or ConnectionRefusedError("refused")
    left = [fails]

    def _fn(role, convo):
        if left[0] > 0:
            left[0] -= 1
            raise exc
        return "FINAL: back"
    return _fn


def test_a_run_with_work_done_waits_for_the_model(_no_sleep):
    fn = _flaky(12)
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, ConnectionRefusedError("refused"),
        None, None, None, wait_s=1800))
    assert out == "FINAL: back"
    assert any("waiting up to 30 min" in e.get("text", "") for e in evs)
    assert not any(e["type"] == "stopped" for e in evs)


def test_the_wait_is_bounded(_no_sleep):
    fn = _flaky(10_000)
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, ConnectionRefusedError("refused"),
        None, None, None, wait_s=300))
    assert out is _completion._RETRY_STOP
    assert any(e["type"] == "stopped" for e in evs)


def test_a_fresh_request_does_not_wait(_no_sleep):
    fn = _flaky(12)
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, ConnectionRefusedError("refused"),
        None, None, None, wait_s=0))
    assert out is _completion._RETRY_STOP
    assert not any("waiting" in e.get("text", "") for e in evs)


@pytest.mark.parametrize("exc", [
    ValueError("bad request shape"),
    RuntimeError("llm.exhausted role=chat — all providers returned transport error"),
])
def test_an_error_that_is_not_an_outage_does_not_wait(_no_sleep, exc):
    fn = _flaky(12, exc=exc)
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, exc, None, None, None, wait_s=1800))
    assert out is _completion._RETRY_STOP
    assert not any("waiting" in e.get("text", "") for e in evs)


def _http(code):
    import io
    import urllib.error
    return urllib.error.HTTPError("http://m/v1", code, "x", {}, io.BytesIO(b"{}"))


def test_what_counts_as_an_outage():
    from aiforge_core.llm.client import _exhausted_error
    from aiforge_core.llm.router import Endpoint
    ep = Endpoint(provider="openai_compatible", base_url="http://127.0.0.1:9",
                  model="m", api_key="", role="chat", extras={})
    dead = _exhausted_error("chat", ep, None, None, 0,
                            {"exc": ConnectionRefusedError("refused")})
    bad = _exhausted_error("chat", ep, None, None, 0, {"exc": _http(400)})
    assert _completion._outage_waitable(dead)          # the text path, tagged
    assert not _completion._outage_waitable(bad)
    assert _completion._outage_waitable(_http(503))
    assert not _completion._outage_waitable(_http(500))
    assert not _completion._outage_waitable(_http(401))


def test_only_an_interactive_run_with_work_done_waits(monkeypatch):
    seen = []

    def fake_retry(*a, wait_s=0.0, **k):
        seen.append(wait_s)
        return _completion._RETRY_STOP
        yield  # pragma: no cover

    def boom(*a, **k):
        raise ConnectionRefusedError("refused")
        yield  # pragma: no cover
    monkeypatch.setattr(_completion, "_retry_completion", fake_retry)
    monkeypatch.setattr(_completion, "_complete_live", boom)
    for sid, counts in ((7, {"x": 1}), (None, {"x": 1}), (7, {})):
        st = SimpleNamespace(convo=[], edits_made=0, action_counts=counts)
        _drive(_completion._run_completion(st, "chat", None, sid, None))
    assert seen[0] > 0 and seen[1] == 0 and seen[2] == 0


def test_stop_during_the_wait_stops_the_run(_no_sleep, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    fn = _flaky(10_000)
    calls = []
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: calls.append(1) or len(calls) > 10)
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], 4242, ConnectionRefusedError("refused"),
        None, None, None, wait_s=1800))
    assert out is _completion._CANCELLED


def test_outage_wait_setting(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_OUTAGE_WAIT_S", raising=False)
    assert _completion._outage_wait_s() == 1800
    monkeypatch.setenv("AIFORGE_CHAT_OUTAGE_WAIT_S", "0")
    assert _completion._outage_wait_s() == 0
    monkeypatch.setenv("AIFORGE_CHAT_OUTAGE_WAIT_S", "-5")
    assert _completion._outage_wait_s() == 1800


# ── stored events stay small ─────────────────────────────────────────────

def test_stored_tool_results_are_cut(monkeypatch):
    from aiforge_core.runtime.chat_event_slim import slim_event
    ev = {"type": "tool", "name": "file_read", "args": {"path": "a"},
          "result": {"ok": True, "content": "x" * 20000, "path": "a"}}
    slim = slim_event(ev)
    assert slim["result"]["ok"] is True and slim["result"]["path"] == "a"
    assert len(slim["result"]["content"]) < 8200
    assert "12000 more characters" in slim["result"]["content"]
    assert len(ev["result"]["content"]) == 20000          # live copy untouched
    thought = {"type": "thought", "text": "y" * 20000}
    assert slim_event(thought) is thought


def test_stored_arguments_and_long_lists_are_cut():
    from aiforge_core.runtime.chat_event_slim import slim_event
    ev = {"type": "tool", "name": "file_write",
          "args": {"path": "a", "content": "c" * 30000},
          "result": {"ok": True, "matches": [str(i) for i in range(1000)],
                     "deep": {"a": {"b": {"c": {"d": "e" * 30000}}}}}}
    slim = slim_event(ev)
    assert len(slim["args"]["content"]) < 8200 and slim["args"]["path"] == "a"
    assert len(slim["result"]["matches"]) == 201
    import json
    assert len(json.dumps(slim["result"]["deep"])) < 9000


def test_the_replay_buffer_keeps_the_cut_copy():
    from aiforge_core.runtime import chat_runs
    run = chat_runs._Run(99991)
    big = {"type": "tool", "name": "file_read", "result": {"content": "z" * 20000}}
    run.publish(big)
    assert len(run.events[0]["result"]["content"]) < 8200
