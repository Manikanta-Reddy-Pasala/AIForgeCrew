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


def test_a_long_edit_and_test_cycle_is_never_a_loop(tmp_path):
    st = _loop_state()
    f = tmp_path / "a.py"
    for i in range(89):
        f.write_text(f"version {i}")
        _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
        assert not _progress.strike(st, "run_command|pytest")
    # …until the lifetime ceiling asks it to step back (not "same args")
    f.write_text("version 89")
    _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
    assert _progress.strike(st, "run_command|pytest") == "often"


@pytest.fixture
def _repo(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text("v1")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return sub


def _shell_ran(st, cwd):
    _progress.note_command(st, "run_command", {"ok": True}, str(cwd))
    _progress._refresh_tree(st)


def test_a_shell_edit_is_a_new_state(_repo):
    """The workspace is a folder inside the repository: git's paths are
    relative to the repository root."""
    st = _loop_state()
    _shell_ran(st, _repo)                          # clean tree: nothing yet
    assert st.new_states == 0
    (_repo / "a.py").write_text("v2 — changed by sed")
    _shell_ran(st, _repo)
    assert st.new_states == 1
    (_repo / "a.py").write_text("v3")              # an already-dirty file again
    _shell_ran(st, _repo)
    assert st.new_states == 2
    _progress.note_command(st, "file_read", {"ok": True}, str(_repo))
    assert not st.tree_pending                     # not a shell command
    assert st.new_states == 2


def test_git_is_asked_only_when_a_repeat_nears_a_loop(_repo, monkeypatch):
    calls = []
    real = _progress._tracked_changes
    monkeypatch.setattr(_progress, "_tracked_changes",
                        lambda st, cwd: calls.append(1) or real(st, cwd))
    st = _loop_state(cwd=str(_repo))
    for _ in range(3):
        _progress.note_command(st, "run_command", {"ok": True}, str(_repo))
        assert not _progress.strike(st, "run_command|pytest")
    assert calls == []
    (_repo / "a.py").write_text("fixed by sed")
    _progress.note_command(st, "run_command", {"ok": True}, str(_repo))
    assert not _progress.strike(st, "run_command|pytest")   # 4th: tree looked at
    assert calls == [1]


def test_edits_that_name_no_file_cannot_keep_a_loop_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "5")
    st = _loop_state()
    hits = []
    for _ in range(20):
        _progress.note_write(st, "rename_symbol", {"old": "a", "new": "b"},
                             {"ok": True}, tmp_path)
        hits.append(bool(_progress.strike(st, "run_command|pytest")))
    assert hits.index(True) == 4                   # the backstop, not reset


def test_the_lifetime_ceiling_holds_across_real_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "5")
    st = _loop_state()
    f = tmp_path / "a.py"
    hits = []
    for i in range(20):
        f.write_text(f"v{i}")
        _progress.note_write(st, "file_write", {"path": "a.py"}, {"ok": True}, tmp_path)
        hits.append(bool(_progress.strike(st, "run_command|pytest")))
    assert hits.index(True) == 14                  # 3 × the backstop


def test_rewriting_the_same_bytes_or_flipping_back_is_not_new(_repo):
    import os
    import time as _t
    st = _loop_state()
    f = _repo / "a.py"
    f.write_text("B")
    _shell_ran(st, _repo)
    seen = st.new_states
    for content in ("B", "C", "B", "C", "B"):
        _t.sleep(0.01)
        f.write_text(content)
        os.utime(f)
        _shell_ran(st, _repo)
    assert st.new_states == seen + 1               # only "C" was new


def test_untracked_output_files_are_not_progress(_repo):
    st = _loop_state()
    for i in range(3):
        (_repo / "report.xml").write_text(f"<run {i}/>")
        _shell_ran(st, _repo)
    assert st.new_states == 0


def test_a_repeated_read_between_new_ones_is_stopped_by_the_backstop(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LOOP_BACKSTOP", "5")
    st = _loop_state()
    hits = [_progress.strike(st, "list_dir|.", per_state=False) for _ in range(5)]
    assert hits == ["", "", "", "", "often"]


def test_closing_a_task_refills_the_recovery_ceiling(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MAX_RECOVERIES", "2")
    st = _loop_state(board={"a": {"title": "a", "status": "pending"}})
    st.recoveries_total = 2
    assert not _progress.may_recover(st)
    st.board["a"]["status"] = "done"
    assert _progress.may_recover(st)
    # flipping the same item back and forth is not more progress
    st.recoveries_total = 2
    st.board["a"]["status"] = "running"
    assert not _progress.may_recover(st)
    st.board["a"]["status"] = "done"
    assert not _progress.may_recover(st)


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
    import os
    os.environ["AIFORGE_CHAT_LOOP_BACKSTOP"] = "10"
    try:
        evs = _run(tmp_path, fn)
    finally:
        del os.environ["AIFORGE_CHAT_LOOP_BACKSTOP"]
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
    # an unknown slug without a title is only a progress flip for the dock,
    # as before: it does not go on the board
    res, evs = _tasks.apply_progress(board, {"slug": "x", "status": "in progress"})
    assert res["ok"] and "x" not in board
    assert evs == [{"type": "subtask_update", "slug": "x", "status": "running"}]


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


def test_plan_mode_keeps_the_planners_panel(tmp_path):
    fn, _ = _scripted([_plan("extra", title="my own step"), "FINAL: the plan"])
    evs = _run(tmp_path, fn, mode="plan")
    assert not [e for e in evs if e["type"] == "subtasks"
                and any(i["slug"] == "extra" for i in e["items"])]
    assert {"type": "subtask_update", "slug": "extra", "status": "pending"} in evs


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
    multi = [{"role": "user", "content": "fix the login bug. also add a retry to "
              "the sync client. and update the README"}]
    hi = [{"role": "user", "content": "hi"}]
    on = _build_convo(multi, str(tmp_path), "chat", unlimited=True, **kw)[0]
    capped = _build_convo(multi, str(tmp_path), "chat", **kw)[0]
    short = _build_convo(hi, str(tmp_path), "chat", unlimited=True, **kw)[0]
    assert "LONG AND MULTI-TASK WORK" in on[0]["content"]
    assert "LONG AND MULTI-TASK WORK" not in capped[0]["content"]
    assert "LONG AND MULTI-TASK WORK" not in short[0]["content"]   # a plain chat


def test_a_condensed_long_run_is_reminded_through_the_pin(tmp_path):
    st = SimpleNamespace(goal="port it", steers=[], cwd=str(tmp_path),
                         file_hashes={}, unlimited=True)
    assert "LONG AND MULTI-TASK WORK" in _tasks.turn_pin(st)
    st.unlimited = False
    assert "LONG AND MULTI-TASK WORK" not in _tasks.turn_pin(st)


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
    for sid, edits in ((7, 1), (None, 1), (7, 0)):
        st = SimpleNamespace(convo=[], edits_made=edits, action_counts={"x": 1})
        _drive(_completion._run_completion(st, "chat", None, sid, None))
    assert seen[0] > 0 and seen[1] == 0 and seen[2] == 0     # a read-only turn fails fast


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


def test_approval_cards_are_stored_whole_unless_huge():
    from aiforge_core.runtime.chat_event_slim import slim_event
    card = {"type": "approval", "args": {"content": "c" * 30000},
            "preview": "p" * 30000}
    assert slim_event(card) == card
    huge = {"type": "approval", "preview": "p" * 3_000_000}
    assert len(slim_event(huge)["preview"]) < 1_100_000


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


# ── long commands ────────────────────────────────────────────────────────

def test_a_command_with_a_lot_of_output_does_not_hang(tmp_path, monkeypatch):
    """A pipe holds about 64 KB; a verbose build used to block on it until
    the timeout killed it."""
    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    cmd = "python3 -c \"import sys; sys.stdout.write('x' * 2_000_000); print('END')\""
    res = _shell._t_run_command({"cmd": cmd, "timeout": 20}, str(tmp_path))
    assert res["ok"] is True, res.get("error")
    assert res["stdout"].rstrip().endswith("END")


def test_runaway_output_is_stopped(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_CMD_OUTPUT_MAX_MB", "1")
    res = _shell._t_run_command(
        {"cmd": "head -c 3000000 /dev/zero; sleep 30", "timeout": 60}, str(tmp_path))
    assert res["ok"] is False and "more than 1 MB" in res["error"]


def test_progress_bars_become_lines(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    res = _shell._t_run_command({"cmd": "printf '10%%\\r50%%\\rdone\\nnext\\n'"},
                                str(tmp_path))
    assert res["stdout"].splitlines() == ["done", "next"]


def _running(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


_LINUX = pytest.mark.skipif(not __import__("os").path.isdir("/proc"),
                            reason="Linux only")


@_LINUX
def test_a_bare_background_child_is_stopped_with_the_command(tmp_path, monkeypatch):
    """It writes into the command's output, as it did into the old pipe."""
    import time as _t

    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    res = _shell._t_run_command(
        {"cmd": "sleep 300 & echo $! > child.pid"}, str(tmp_path))
    assert res["ok"] is True
    pid = int((tmp_path / "child.pid").read_text())
    for _ in range(50):
        if not _running(pid):
            break
        _t.sleep(0.1)
    else:
        pytest.fail("the background child is still running")


@_LINUX
def test_a_redirected_background_child_keeps_running(tmp_path, monkeypatch):
    import os
    import signal

    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    res = _shell._t_run_command(
        {"cmd": "sleep 300 > app.log 2>&1 & echo $! > child.pid"}, str(tmp_path))
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert res["ok"] is True
        assert _running(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_a_timed_out_command_still_shows_its_output(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _shell
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    res = _shell._t_run_command(
        {"cmd": "echo started; sleep 30", "timeout": 1}, str(tmp_path))
    assert res["timed_out"] is True
    assert "started" in res["stdout"]
