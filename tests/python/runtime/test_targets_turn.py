import types
import pytest
from aiforge_core.runtime import targets as T
from aiforge_core.runtime.chat_agent._turn import _tasks, _action

JSON = '{"a": 1}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


def _seed(sid=5):
    T.capture(sid, "match this output:\n```\n" + JSON + "\n```")
    board = _tasks.seed_board(["make the endpoint"], "")
    board.update(T.board_items(sid))
    return board


def test_enforced_item_refuses_done_from_the_model():
    board = _seed()
    res, evs = _tasks.apply_progress(board, {"slug": "target-1", "status": "done"})
    assert res["ok"] is False and "compare" in res["error"]
    assert board["target-1"]["status"] == "pending"
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "status": "running"})
    assert res["ok"] and board["target-1"]["status"] == "running"
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "status": "done", "_harness": True})
    assert res["ok"] and board["target-1"]["status"] == "done"


def test_enforced_item_cannot_be_removed():
    board = _seed()
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "remove": True})
    assert res["ok"] is False and "target-1" in board


def test_a_passing_compare_closes_the_item_and_marks_verified():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    evs = _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                       {"ok": True, "code": 0, "stdout": ""})
    assert board["target-1"]["status"] == "done"
    assert T.list(5)[0]["verified"]["evidence"].startswith("diff out.json")
    assert evs and evs[0]["type"] == "subtask_update" and evs[0]["status"] == "done"


def test_a_failing_compare_keeps_the_item_open_and_says_differs():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    evs = _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                       {"ok": False, "code": 1, "stdout": "< a\n> b"})
    assert evs == []
    assert board["target-1"]["status"] == "pending" and T.list(5)[0]["verified"] is None
    assert T.facts(5) == ["target-1: differs"]
    last = T.list(5)[0]["last_compare"]
    assert last["ok"] is False and "exit 1" in last["evidence"] and "< a" in last["evidence"]


def test_a_passing_compare_after_a_failing_one_closes_the_item():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                 {"ok": False, "code": 1, "stdout": "< a\n> b"})
    evs = _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                       {"ok": True, "code": 0, "stdout": ""})
    assert board["target-1"]["status"] == "done"
    assert T.facts(5) == ["target-1: matched"]
    assert evs and evs[0]["status"] == "done"


def test_a_stopped_or_still_running_compare_proves_nothing():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    cmd = f"diff out.json {p}"
    assert _action._note_target_compare(st, "run_command", {"cmd": cmd},
                                        {"ok": False, "running": True, "id": "bg-9"}) == []
    assert _action._note_target_compare(st, "run_command", {"cmd": cmd},
                                        {"ok": False, "code": -15, "stopped": True}) == []
    assert board["target-1"]["status"] == "pending"
    assert T.facts(5) == ["target-1: not yet compared"]


def test_a_compare_left_running_and_checked_on_later_still_closes():
    from aiforge_core.runtime import cmd_finished
    cmd_finished.reset()
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    cmd = f"diff out.json {p}"
    # The hand-off: the compare was started and left running.
    assert _action._note_target_compare(st, "run_command", {"cmd": cmd},
                                        {"ok": True, "running": True, "id": "bg-1"}) == []
    assert board["target-1"]["status"] == "pending"
    # It ended; a check-in reports the recorded end under the original command.
    cmd_finished.record("bg-1", cmd, 0, "", "")
    evs = _action._note_target_compare(st, "command_wait", {"id": "bg-1"},
                                       {"ok": True, "running": False, "id": "bg-1"})
    assert board["target-1"]["status"] == "done"
    assert evs and evs[0]["slug"] == "target-1" and evs[0]["status"] == "done"
    assert T.list(5)[0]["verified"]["evidence"].startswith("diff out.json")
    cmd_finished.reset()


def test_turn_pin_carries_the_targets_block():
    _seed()
    st = types.SimpleNamespace(goal="go", steers=[], cwd="/x", file_hashes={}, unlimited=True, session_id=5)
    pin = _tasks.turn_pin(st)
    assert "TARGETS" in pin and "target-1" in pin


def test_handoff_render_lists_targets():
    from aiforge_core.runtime import handoff
    _seed()
    st = types.SimpleNamespace(goal="go", board={}, session_id=5, convo=[], file_hashes={},
                               steers=[], edits_made=0, cwd="/x")
    h = handoff.build_chat(st)
    assert len(h["targets"]) == 1
    assert h["targets"][0].startswith("target-1 (output, ")
    assert h["targets"][0].endswith("target-1.json): not yet compared")
    assert "target-1" in handoff.render(h)


def test_facts_line_mentions_targets(monkeypatch):
    from aiforge_core.runtime import turn_facts_line as F, action_log
    _seed()
    monkeypatch.setattr(action_log, "live_steps", lambda _s: [{"type": "tool", "name": "run_command",
                                                               "args": {"cmd": "ls"}, "result": {"ok": True}}])
    monkeypatch.setattr(F, "_changed", lambda *a, **k: set())
    assert "target-1: not yet compared" in F.suffix(5, "/x", None)


def test_done_on_an_item_the_harness_already_closed_is_accepted():
    """After a passing compare the harness closes the item; the model's own
    "done" for it then is a no-op, not a refusal it keeps retrying."""
    board = _seed()
    board["target-1"]["status"] = "done"
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "status": "done"})
    assert res["ok"] and board["target-1"]["status"] == "done"
