"""The chat agent keeps going on long, multi-task runs.

Four things used to end or derail a run that had been working for hours: the
loop guard counted a test command re-run after every fix as a loop; the
stuck-recovery budget was spent once per run, however much progress came in
between; the model's plan was condensed away with the old messages; and a
model outage of two minutes ended the turn.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime.chat_agent._turn import _completion, _limits, _tasks


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


def test_progress_refills_the_recovery_budget():
    st = SimpleNamespace(edits_made=0, reads_new=0, stuck_recoveries=0)
    _limits.refill_recoveries(st)
    st.stuck_recoveries = 3
    _limits.refill_recoveries(st)             # no progress since: still spent
    assert st.stuck_recoveries == 3
    st.edits_made = 1
    _limits.refill_recoveries(st)
    assert st.stuck_recoveries == 0


# ── the task board ───────────────────────────────────────────────────────

def test_the_model_adds_and_updates_its_own_items():
    board = _tasks.seed_board(["fix the bug"])
    res, evs = _tasks.apply_progress(board, {"slug": "docs", "title": "Update docs"})
    assert res["ok"] and board["docs"]["status"] == "pending"
    assert evs[0]["type"] == "subtasks"
    assert [i["slug"] for i in evs[0]["items"]] == ["part-1", "docs"]
    res, evs = _tasks.apply_progress(board, {"slug": "docs", "status": "done"})
    assert board["docs"]["status"] == "done"
    assert evs == [{"type": "subtask_update", "slug": "docs", "status": "done"}]
    assert _tasks.open_items(board) == ["part-1"]
    assert _tasks.open_planned(board) == []


def test_bad_progress_calls_are_refused():
    board = {}
    assert not _tasks.apply_progress(board, {})[0]["ok"]
    assert not _tasks.apply_progress(board, {"slug": "x", "status": "nope"})[0]["ok"]
    # an unknown slug without a title is still accepted, as before
    assert _tasks.apply_progress(board, {"slug": "x"})[0]["ok"]


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


def _progress(slug, **kw):
    import json
    return f"ACTION: plan_progress\nARGS_JSON: {json.dumps({'slug': slug, **kw})}"


def test_final_with_open_planned_items_keeps_going(tmp_path):
    fn, calls = _scripted([
        _progress("one", title="first thing"),
        _progress("two", title="second thing"),
        _progress("one", status="done"),
        "FINAL: did the first thing",
        _progress("two", status="done"),
        "FINAL: did both",
    ])
    evs = _run(tmp_path, fn)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "did both"
    reminder = "\n".join(str(m["content"]) for m in calls[4])
    assert "two: second thing" in reminder and "one: first thing" not in reminder
    docks = [e for e in evs if e["type"] == "subtasks"]
    assert [i["slug"] for i in docks[-1]["items"]] == ["one", "two"]


def test_the_reminder_is_bounded(tmp_path):
    fn, _ = _scripted([_progress("one", title="never done")] + ["FINAL: gave up"] * 8)
    evs = _run(tmp_path, fn)
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "gave up"
    nudges = [e for e in evs if "still open" in str(e.get("text", ""))]
    assert len(nudges) == 2


def test_plan_mode_is_not_pushed_to_do_the_plan(tmp_path):
    fn, _ = _scripted([_progress("one", title="later"), "FINAL: the plan"])
    evs = _run(tmp_path, fn, mode="plan")
    assert [e for e in evs if e["type"] == "message"][-1]["text"] == "the plan"


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


def test_a_non_transient_error_does_not_wait(_no_sleep):
    fn = _flaky(12, exc=ValueError("bad request shape"))
    evs, out = _drive(_completion._retry_completion(
        fn, "chat", [], None, ValueError("bad request shape"),
        None, None, None, wait_s=1800))
    assert out is _completion._RETRY_STOP


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


def test_the_replay_buffer_keeps_the_cut_copy():
    from aiforge_core.runtime import chat_runs
    run = chat_runs._Run(99991)
    big = {"type": "tool", "name": "file_read", "result": {"content": "z" * 20000}}
    run.publish(big)
    assert len(run.events[0]["result"]["content"]) < 8200
