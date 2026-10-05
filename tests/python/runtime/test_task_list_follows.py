"""The task list follows the work: a mid-run message is checked against it,
and nothing looks busy after the turn."""
from types import SimpleNamespace

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import chat_interject
from aiforge_core.runtime.chat_agent._turn import _tasks as T

_SID = 771_201


def _board():
    return {"part-1": {"title": "add subtract", "status": "done", "from_request": True},
            "part-2": {"title": "add power", "status": "pending", "from_request": True}}


def test_no_board_no_note():
    assert T.steer_note({}) == ""


def test_the_note_shows_the_board_and_how_to_change_it():
    note = T.steer_note(_board())
    assert "[x] part-1: add subtract" in note and "[ ] part-2: add power" in note
    assert "plan_progress" in note and "skipped" in note
    assert note.startswith("[task board — not the user]")


def test_a_mid_run_message_reaches_the_model_with_the_board(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    seen: list = []

    def fn(role, messages, **kw):
        seen.append("\n".join(str(m.get("content")) for m in messages
                              if m.get("role") == "user"))
        if len(seen) == 1:
            chat_interject.push(_SID, "skip the second, add modulo instead")
            return 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'
        return "done"

    chat_interject.clear(_SID)
    list(ca.run_chat_agent(
        [{"role": "user", "content": "1. add subtract to calc.py\n2. add power to calc.py\n3. run the tests"}],
        cwd=str(tmp_path), complete_fn=fn, session_id=_SID))
    chat_interject.clear(_SID)
    assert "skip the second, add modulo instead" in seen[-1]
    assert "Your task board as it stands" in seen[-1]


def test_the_turn_is_over_nothing_looks_busy():
    from aiforge_core.api.routes._chat import _turn_events as te
    sent: list = []
    run = SimpleNamespace(publish=sent.append)
    st = {"subtasks": [{"slug": "a", "status": "done"}, {"slug": "b", "status": "running"},
                       {"slug": "c", "status": "pending"}, {"slug": "d", "status": "planned"},
                       {"slug": "e", "status": "failed"}]}
    te._settle_task_list(st, run)
    assert [r["status"] for r in st["subtasks"]] == ["done", "left", "left", "planned", "failed"]
    assert sent == [{"type": "subtask_update", "slug": "b", "status": "left"},
                    {"type": "subtask_update", "slug": "c", "status": "left"}]
    te._settle_task_list({"subtasks": []}, run)


def test_a_broken_stream_still_settles_the_list():
    from aiforge_core.api.routes._chat import _turn_events as te
    sent: list = []
    run = SimpleNamespace(publish=sent.append)
    st = {"subtasks": [{"slug": "a", "status": "running"}], "awaiting": False}
    steps: list = []

    def boom():
        raise RuntimeError("stream died")
        yield  # pragma: no cover

    te._drive_produce_stream(boom, st, steps, run, 7, 0.0, "simple", None,
                             lambda *a, **k: None)
    assert st["subtasks"][0]["status"] == "left"
    assert steps[0] == {"type": "subtasks", "items": st["subtasks"]}
    assert sent[-1] == {"type": "done"}
