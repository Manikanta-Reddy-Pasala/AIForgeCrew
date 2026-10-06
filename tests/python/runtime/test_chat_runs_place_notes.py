"""What is said beside a run is stored where it was said, not after the work."""
from __future__ import annotations


def _tool(n):
    return {"type": "tool", "name": f"t{n}", "args": {}, "result": {"ok": True}}


def _reply(n):
    return {"type": "message", "supplementary": True, "role": "reply", "text": f"r{n}"}


def test_notes_are_placed_between_the_tool_calls_they_were_said_between():
    from aiforge_core.runtime import chat_runs
    sid = 987601
    chat_runs.start(sid)
    try:
        assert chat_runs.note(sid, _reply(0))                 # before any tool
        chat_runs.publish(sid, _tool(1))
        assert chat_runs.note(sid, _reply(1))
        assert chat_runs.note(sid, _reply(2))
        chat_runs.publish(sid, _tool(2))
        chat_runs.publish(sid, _tool(3))
        assert chat_runs.note(sid, _reply(3))                 # after the last tool
        steps = [{"type": "thought", "text": "a"}, _tool(1), _tool(2),
                 {"type": "thought", "text": "b"}, _tool(3)]
        chat_runs.place_notes(sid, steps)
        seen = [s.get("text") or s.get("name") for s in steps]
        assert seen == ["a", "r0", "t1", "r1", "r2", "t2", "b", "t3", "r3"]
        assert chat_runs.take_notes(sid) == []                # taken once
    finally:
        chat_runs.finish(sid)


def test_no_run_changes_nothing():
    from aiforge_core.runtime import chat_runs
    steps = [_tool(1)]
    chat_runs.place_notes(987602, steps)
    assert steps == [_tool(1)]


def test_a_note_said_while_a_tool_runs_goes_after_that_tool():
    from aiforge_core.runtime import chat_runs
    sid = 987603
    chat_runs.start(sid)
    try:
        chat_runs.publish(sid, {"type": "tool_start", "name": "t1", "args": {}, "call_id": 1})
        assert chat_runs.note(sid, _reply(1))
        chat_runs.publish(sid, {**_tool(1), "call_id": 1})
        chat_runs.publish(sid, _tool(2))
        steps = [_tool(1), _tool(2)]
        chat_runs.place_notes(sid, steps)
        assert [s.get("text") or s.get("name") for s in steps] == ["t1", "r1", "t2"]
    finally:
        chat_runs.finish(sid)
