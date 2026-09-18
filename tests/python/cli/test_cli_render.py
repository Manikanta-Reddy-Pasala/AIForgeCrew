"""The renderer as a pure function: events in, text out, no terminal."""

from __future__ import annotations

from aiforge_cli.colors import Palette
from aiforge_cli.render import Renderer

PLAIN = Palette(False)


def _run(events, verbosity=0):
    r = Renderer(PLAIN, verbosity=verbosity)
    r.begin_turn()
    lines, streamed = [], ""
    for ev in events:
        op = r.handle(ev)
        lines += op.lines
        streamed += op.stream
    return r, lines, streamed


TRANSCRIPT = [
    {"type": "thought", "text": "reading PosServerBackendService"},
    {"type": "tool_start", "name": "read_file", "args": {"path": "A.java"}},
    {"type": "tool", "name": "read_file", "args": {"path": "A.java"},
     "result": {"lines": 312}, "duration_s": 0.4, "call_id": 1},
    {"type": "usage", "pct": 18.0, "llmTurn": 3},
    {"type": "delta", "text": "Added "},
    {"type": "delta", "text": "retry."},
    {"type": "message", "text": "Added retry."},
    {"type": "done", "elapsed_s": 102.0},
]


def test_the_tool_count_and_files_are_not_doubled_by_a_replay():
    r = Renderer(PLAIN)
    r.begin_turn()
    for ev in TRANSCRIPT[:4]:
        r.handle(ev)
    r.begin_replay()
    lines = []
    for ev in TRANSCRIPT:
        lines += r.handle(ev).lines
    done = [line for line in lines if line.startswith("done")]
    assert done
    assert "1 tool" in done[0]


def test_a_changed_file_set_is_summarised():
    _, lines, _ = _run([{"type": "changes",
                         "files": [{"path": "A.java", "status": "modified",
                                    "additions": 3, "deletions": 1}],
                         "summary": {"files": 1, "additions": 3, "deletions": 1}},
                        {"type": "done"}])
    body = "\n".join(lines)
    assert "1 file" in body
    assert "A.java" in body
    assert "+3 -1" in body


def test_a_whole_turn_renders_one_line_per_step():
    _, lines, streamed = _run(TRANSCRIPT)
    text = "\n".join(lines)
    assert "● thinking reading PosServerBackendService" in text
    assert "✓ read_file" in text
    assert "312 lines" in text
    assert "0.4s" in text
    assert streamed == "Added retry."
    assert "done" in text
    assert "1m42s" in text
    assert "1 tool" in text
    assert "ctx 18%" in text


def test_the_final_message_is_not_printed_twice_after_streaming():
    _, lines, streamed = _run(TRANSCRIPT)
    assert streamed.count("Added retry.") == 1
    assert "\n".join(lines).count("Added retry.") == 0


def test_a_message_that_extends_the_stream_continues_the_same_line():
    events = [{"type": "delta", "text": "Added "},
              {"type": "message", "text": "Added retry to the push sync."}]
    r = Renderer(PLAIN)
    r.begin_turn()
    streamed = ""
    lines = []
    for ev in events:
        op = r.handle(ev)
        streamed += op.stream
        lines += op.lines
    # As a `lines` entry the remainder would close the streamed row first and
    # split the answer mid-sentence.
    assert streamed == "Added retry to the push sync."
    assert lines == [""]


def test_the_final_usage_event_does_not_wipe_the_context_meter():
    # The last usage event carries only the llm_* counters.
    r = Renderer(PLAIN)
    r.begin_turn()
    r.handle({"type": "usage", "pct": 42.0, "window_tokens": 128000,
              "context_tokens": 53000})
    r.handle({"type": "usage", "llm_turn": 9, "final": True})
    done = r.handle({"type": "done", "elapsed_s": 2.0})
    assert "ctx 42%" in done.lines[0]


def test_the_context_meter_reports_tokens_as_well_as_a_percentage():
    r = Renderer(PLAIN)
    r.begin_turn()
    op = r.handle({"type": "usage", "pct": 18.0, "window_tokens": 128000,
                   "context_tokens": 23000, "llm_session": 7})
    assert "ctx 18% (23k/128k)" in op.tail
    assert "req 7" in op.tail


def test_quiet_still_prints_the_answer_after_a_reconnect():
    r = Renderer(PLAIN, verbosity=-1)
    r.begin_turn()
    r.begin_replay()
    out = ""
    lines = []
    for ev in [{"type": "delta", "text": "the "}, {"type": "delta", "text": "answer"},
               {"type": "message", "text": "the answer"}]:
        op = r.handle(ev)
        out += op.stream
        lines += op.lines
    assert "the answer" in out + "\n".join(lines)


def test_two_identical_untagged_calls_are_both_counted():
    events = [{"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
               "result": {}},
              {"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
               "result": {}},
              {"type": "done"}]
    _, lines, _ = _run(events)
    assert len([li for li in lines if "run_command" in li]) == 2
    assert "2 tools" in lines[-1]


def test_a_failed_tool_is_marked_and_counted():
    _, lines, _ = _run([
        {"type": "tool", "name": "run_command", "args": {"cmd": "mvn"},
         "result": {"error": "exit 1: compile failed"}, "call_id": 7},
        {"type": "done"},
    ])
    assert "✗ run_command" in lines[0]
    assert "compile failed" in lines[0]
    assert "1 failed" in lines[-1]


def test_replayed_events_do_not_double_the_transcript():
    # A dropped stream re-attaches; the API replays the run from its start.
    r = Renderer(PLAIN)
    r.begin_turn()
    first = []
    for ev in TRANSCRIPT[:5]:
        op = r.handle(ev)
        first += op.lines
    streamed_before = r.handle(TRANSCRIPT[5]).stream

    r.begin_replay()
    again, streamed_after = [], ""
    for ev in TRANSCRIPT:
        op = r.handle(ev)
        again += op.lines
        streamed_after += op.stream

    assert streamed_before == "retry."
    assert "read_file" not in "\n".join(again)          # already on screen
    assert streamed_after == ""                          # nothing owed to the screen


def test_quiet_prints_the_answer_and_nothing_else():
    _, quiet_lines, quiet_stream = _run(TRANSCRIPT, verbosity=-1)
    assert not any("read_file" in line for line in quiet_lines)
    assert not any("thinking" in line for line in quiet_lines)
    # "answers only" means the answer IS printed: accumulating the deltas
    # without emitting them made the final message look already-on-screen.
    assert "Added retry." in quiet_stream + "\n".join(quiet_lines)


def test_verbose_prints_the_result():
    _, loud_lines, _ = _run(TRANSCRIPT, verbosity=1)
    assert any('"lines": 312' in line for line in loud_lines)


def test_a_diff_result_is_shown_with_its_hunks():
    _, lines, _ = _run([{"type": "tool", "name": "edit_file", "args": {"path": "M.java"},
                         "result": {"diff": "@@ -1 +1 @@\n-old\n+new",
                                    "additions": 1, "deletions": 1}, "call_id": 2}])
    body = "\n".join(lines)
    assert "+1 -1" in body
    assert "@@ -1 +1 @@" in body
    assert "+new" in body


def test_an_unknown_event_never_raises():
    r = Renderer(PLAIN, verbosity=1)
    r.begin_turn()
    op = r.handle({"type": "a_future_event", "payload": 1})
    assert op.lines
    assert "a_future_event" in op.lines[0]
    assert Renderer(PLAIN).handle({"type": "a_future_event"}).lines == []


def test_an_approval_asks_and_carries_the_preview():
    r = Renderer(PLAIN)
    r.begin_turn()
    op = r.handle({"type": "approval", "id": 3, "tool": "file_write",
                   "args": {"path": "x"}, "preview": "@@\n+one"})
    assert op.approval is not None
    assert op.approval["id"] == 3
    assert any("+one" in line for line in op.lines)


def test_tool_arguments_are_ellipsized_in_the_middle():
    long_path = "/very/long/path/" + "x" * 200 + "/Target.java"
    _, lines, _ = _run([{"type": "tool", "name": "read_file",
                         "args": {"path": long_path}, "result": {}, "call_id": 4}])
    assert "…" in lines[0]
    assert "Target.java" in lines[0]       # the end survives, which is the point


def test_the_status_line_colours_context_by_pressure():
    assert PLAIN.ctx(10) == "ok"
    assert PLAIN.ctx(70) == "warn"
    assert PLAIN.ctx(90) == "fail"


def test_a_thought_repeating_the_streamed_text_is_not_printed_twice():
    r = Renderer(PLAIN)
    r.begin_turn()
    r.handle({"type": "delta", "text": "Let me read the config file first."})
    op = r.handle({"type": "thought", "text": "Let me read the config file first."})
    assert not op.lines
    op = r.handle({"type": "thought", "role": "system", "text": "⧗ running the checks"})
    assert op.lines
