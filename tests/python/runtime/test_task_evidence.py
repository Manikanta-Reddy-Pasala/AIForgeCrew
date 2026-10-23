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
    assert board_items(st.board)[0]["evidence"] is ev


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
