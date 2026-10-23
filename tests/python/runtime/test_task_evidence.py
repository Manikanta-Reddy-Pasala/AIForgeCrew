"""A board item that closes carries the evidence: what the run did while it
was open (commands with exit codes and output tails, edits, reads), beside the
item's own check — pinned by the harness, streamed to the UI and kept with the
task panel."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _evidence as E
from aiforge_core.runtime.chat_agent._turn._action import _with_evidence
from aiforge_core.runtime.chat_agent._turn._tasks import apply_progress, board_items


def _st(**board):
    return SimpleNamespace(board={k: dict(v) for k, v in board.items()})


def _progress(st, **args):
    before = {s: it.get("status") for s, it in st.board.items()}
    result, events = apply_progress(st.board, args)
    return result, _with_evidence(st, result, events, before)


def test_check_is_read_from_the_title():
    assert E.check_of("Add div guard — check: pytest passes") == "pytest passes"
    assert E.check_of("Add div guard") == ""


def test_a_closed_item_carries_what_ran_since_it_started():
    st = _st(**{"part-1": {"title": "Fix div — check: tests pass", "status": "pending"}})
    _progress(st, slug="part-1", status="running")
    E.note(st, "file_read", {"path": "calc/ops.py"}, {"ok": True, "content": "x"})
    E.note(st, "file_patch", {"path": "calc/ops.py"}, {"ok": True})
    E.note(st, "run_command", {"cmd": "python3 -m pytest -q"},
           {"ok": True, "code": 0, "stdout": "....\n4 passed in 0.1s\n", "stderr": ""})
    E.note(st, "plan_progress", {"slug": "x"}, {"ok": True})        # bookkeeping: skipped
    result, events = _progress(st, slug="part-1", status="done")
    ev = st.board["part-1"]["evidence"]
    assert ev["check"] == "tests pass" and ev["status"] == "done"
    assert [s["tool"] for s in ev["steps"]] == ["file_read", "file_patch", "run_command"]
    run = ev["steps"][-1]
    assert run["code"] == 0 and "4 passed" in run["output"]
    assert ev["summary"] == "1 command, last exit 0 · 1 edit"
    upd = [e for e in events if e["type"] == "subtask_update"]
    assert upd and upd[0]["evidence"] is ev
    assert "evidence" not in board_items(st.board)[0]     # only on its update


def test_an_item_closed_with_nothing_done_says_so():
    st = _st(a={"title": "Do a", "status": "running"})
    _progress(st, slug="a", status="done")
    ev = st.board["a"]["evidence"]
    assert ev["steps"] == [] and "nothing was run" in ev["note"]


def test_items_closed_together_share_the_work():
    st = _st(a={"title": "A", "status": "running"}, b={"title": "B", "status": "running"})
    E.note(st, "run_command", {"cmd": "make"}, {"ok": True, "code": 0, "stdout": "ok"})
    _progress(st, slug="a", status="done")
    _progress(st, slug="b", status="done")
    evb = st.board["b"]["evidence"]
    assert evb["shared_with"] == "a" and evb["steps"][0]["what"] == "make"
    assert "note" not in evb


def test_a_failed_command_and_a_failed_item():
    st = _st(a={"title": "A", "status": "running"})
    E.note(st, "run_command", {"cmd": "npm test"},
           {"ok": False, "code": 1, "stdout": "", "stderr": "1 failing"})
    _progress(st, slug="a", status="failed")
    ev = st.board["a"]["evidence"]
    assert ev["status"] == "failed" and "1 failed" in ev["summary"]
    assert "[stderr]\n1 failing" in ev["steps"][0]["output"]


def test_starting_an_item_begins_a_fresh_record():
    st = _st(a={"title": "A", "status": "pending"}, b={"title": "B", "status": "pending"})
    E.note(st, "run_command", {"cmd": "echo before"}, {"ok": True, "code": 0})
    _progress(st, slug="b", status="running")
    E.note(st, "run_command", {"cmd": "echo for-b"}, {"ok": True, "code": 0})
    _progress(st, slug="b", status="done")
    assert [s["what"] for s in st.board["b"]["evidence"]["steps"]] == ["echo for-b"]


def test_long_output_keeps_its_end_and_steps_are_capped():
    st = _st(a={"title": "A", "status": "running"})
    for i in range(30):
        E.note(st, "file_read", {"path": f"f{i}"}, {"ok": True})
    E.note(st, "run_command", {"cmd": "big"},
           {"ok": True, "code": 0, "stdout": "x" * 5000 + "THE END"})
    _progress(st, slug="a", status="done")
    steps = st.board["a"]["evidence"]["steps"]
    assert len(steps) == E.MAX_STEPS and steps[-1]["what"] == "big"
    assert steps[-1]["output"].endswith("THE END") and len(steps[-1]["output"]) <= E.TAIL_CHARS + 1


def test_status_unchanged_adds_nothing():
    st = _st(a={"title": "A", "status": "done"})
    _progress(st, slug="a", status="done")
    assert "evidence" not in st.board["a"]


def test_the_panel_keeps_evidence_after_the_turn():
    from aiforge_core.api.routes._chat import _turn_events as T
    import inspect
    src = inspect.getsource(T)
    assert 'row["evidence"] = ev["evidence"]' in src


def test_the_answer_carries_the_turns_work_when_no_task_did():
    st = _st()
    E.note(st, "file_patch", {"path": "a.py"}, {"ok": True})
    E.note(st, "run_command", {"cmd": "pytest -q"}, {"ok": True, "code": 0, "stdout": "4 passed"})
    ev = E.for_answer(st)
    assert [s["tool"] for s in ev["steps"]] == ["file_patch", "run_command"]
    assert ev["summary"] == "1 command, last exit 0 · 1 edit"


def test_the_answer_shows_only_what_no_task_shows():
    st = _st(a={"title": "A", "status": "running"})
    E.note(st, "run_command", {"cmd": "make"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    assert E.for_answer(st) is None                       # all of it is under task a
    E.note(st, "run_command", {"cmd": "git status"}, {"ok": True, "code": 0})
    assert [s["what"] for s in E.for_answer(st)["steps"]] == ["git status"]


def test_a_plain_answer_has_no_evidence():
    assert E.for_answer(_st()) is None


def test_parts_closed_at_final_share_the_turns_work_once():
    st = _st(**{"part-1": {"title": "Fix it — check: tests pass", "status": "running"},
                "part-2": {"title": "Test it", "status": "running"}})
    E.note(st, "run_command", {"cmd": "pytest"}, {"ok": True, "code": 0})
    got = E.for_parts(st, ["part-1", "part-2", "missing"])
    assert [s for s, _ in got] == ["part-1", "part-2"]
    assert st.board["part-1"]["evidence"]["check"] == "tests pass"
    assert st.board["part-1"]["evidence"]["steps"][0]["what"] == "pytest"
    assert st.board["part-2"]["evidence"]["shared_with"] == "part-1"
    assert E.for_answer(st) is None                         # not shown a third time
    assert E.for_parts(st, ["part-1"]) == []                # not twice


def test_a_reopened_item_loses_its_old_evidence():
    st = _st(a={"title": "A", "status": "running"})
    E.note(st, "run_command", {"cmd": "make"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    assert "evidence" in st.board["a"]
    _, events = _progress(st, slug="a", status="running")
    assert "evidence" not in st.board["a"]


def test_two_items_running_each_keep_their_own_work():
    st = _st(a={"title": "A", "status": "pending"}, b={"title": "B", "status": "pending"})
    _progress(st, slug="a", status="running")
    E.note(st, "run_command", {"cmd": "for-a"}, {"ok": True, "code": 0})
    _progress(st, slug="b", status="running")
    E.note(st, "run_command", {"cmd": "for-b"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    _progress(st, slug="b", status="done")
    assert [x["what"] for x in st.board["a"]["evidence"]["steps"]] == ["for-a", "for-b"]
    assert [x["what"] for x in st.board["b"]["evidence"]["steps"]] == ["for-b"]


def test_work_between_items_goes_under_the_answer():
    st = _st(a={"title": "A", "status": "running"}, b={"title": "B", "status": "pending"})
    E.note(st, "run_command", {"cmd": "for-a"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    E.note(st, "run_command", {"cmd": "stray"}, {"ok": False, "code": 2})
    _progress(st, slug="b", status="running")
    E.note(st, "run_command", {"cmd": "for-b"}, {"ok": True, "code": 0})
    _progress(st, slug="b", status="done")
    ans = E.for_answer(st)
    assert [x["what"] for x in ans["steps"]] == ["stray"] and "1 failed" in ans["summary"]


def test_a_failure_is_never_cut_from_the_evidence():
    st = _st(a={"title": "A", "status": "running"})
    E.note(st, "run_command", {"cmd": "pytest"}, {"ok": False, "code": 1, "stdout": "1 failed"})
    for i in range(15):
        E.note(st, "run_command", {"cmd": f"ok{i}"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    ev = st.board["a"]["evidence"]
    assert ev["steps"][0]["what"] == "pytest" and len(ev["steps"]) == E.MAX_STEPS
    assert ev["shown_of"] == [E.MAX_STEPS, 16]
    assert ev["summary"] == "16 commands, last exit 0 · 1 failed"


def test_a_job_peek_is_not_the_last_verdict():
    st = _st(a={"title": "A", "status": "running"})
    E.note(st, "run_command", {"cmd": "npm test"}, {"ok": False, "code": 1})
    E.note(st, "command_output", {"id": "bg-1"}, {"ok": True})
    _progress(st, slug="a", status="done")
    assert "last exit 1" in st.board["a"]["evidence"]["summary"]


def test_tool_kinds():
    assert E._kind("str_replace", {}) == "edit" and E._kind("write_file", {}) == "edit"
    assert E._kind("git_commit", {}) == "edit"
    assert E._kind("shell", {}) == "command" and E._kind("ui_check", {}) == "command"
    assert E._kind("editor", {"command": "view", "path": "a"}) == "read"
    assert E._what("editor", {"command": "str_replace", "path": "a.py"}) == "a.py"


def test_a_harness_closed_target_gets_evidence():
    from aiforge_core.runtime.chat_agent._turn import _action as A
    import inspect
    assert "on_status(st, tid, before, \"done\")" in inspect.getsource(A)


def test_run_tests_exit_code_counts():
    row = E.record("run_tests", {"mode": "fast"}, {"ok": True, "exit_code": 0, "stdout": "4 passed"})
    assert row["code"] == 0 and row["kind"] == "command" and "4 passed" in row["output"]


def test_a_very_long_turn_keeps_its_failures(monkeypatch):
    monkeypatch.setattr(E, "MAX_ROWS", 20)
    st = _st(a={"title": "A", "status": "pending"})
    _progress(st, slug="a", status="running")
    E.note(st, "run_command", {"cmd": "boom"}, {"ok": False, "code": 1})
    for i in range(40):
        E.note(st, "run_command", {"cmd": f"ok{i}"}, {"ok": True, "code": 0})
    _progress(st, slug="a", status="done")
    ev = st.board["a"]["evidence"]
    assert ev["steps"][0]["what"] == "boom" and "1 failed" in ev["summary"]
    assert ev["lost"] == 21 and ev["shown_of"][1] == 41


def test_a_refused_reopen_still_drops_old_evidence():
    st = _st(a={"title": "A", "status": "failed", "evidence": {"status": "failed"}})
    st.board["a"]["status"] = "running"          # what review_progress does on refusal
    _with_evidence(st, {"ok": False, "slug": "a"}, [], {"a": "failed"})
    assert "evidence" not in st.board["a"]


def test_no_empty_shared_line_when_nothing_ran():
    st = _st(**{"part-1": {"title": "A", "status": "running"},
                "part-2": {"title": "B", "status": "running"}})
    got = dict(E.for_parts(st, ["part-1", "part-2"]))
    assert "shared_with" not in got["part-2"] and got["part-2"]["note"]
