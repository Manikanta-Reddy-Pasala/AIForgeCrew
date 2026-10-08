"""The one-call check at the end of a turn: is the request done?"""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _done_check, _plan_first


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_DONE_CHECK", "1")


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _st(**kw):
    base = dict(convo=[], session_id=7, goal="fix the errors one by one, do not give up",
                role="chat", action_counts={"run_command|make": 1})
    return SimpleNamespace(**{**base, **kw})


def _model(monkeypatch, reply, seen=None):
    from aiforge_core.llm import client

    def fake(role, messages, **kw):
        if seen is not None:
            seen.append((role, messages, kw))
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(client, "complete", fake)


def test_an_unfinished_reply_is_sent_back(monkeypatch):
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    st = _st()
    reply = "Here is what I found so far: two of the five errors are fixed."
    events, sig = _drain(_done_check.gate(st, {"text": reply}))
    assert sig == "continue" and st.convo[-1]["content"] == _done_check.NUDGE
    role, messages, kw = seen[0]
    assert role == "chat" and "do not give up" in messages[1]["content"]
    assert reply in messages[1]["content"] and "run_command" in messages[1]["content"]
    assert any("not finished" in e.get("text", "") for e in events)


def test_done_needs_user_and_an_unclear_word_end_the_turn(monkeypatch):
    for word in ("DONE", "NEEDS_USER", "needs user", "maybe", ""):
        _model(monkeypatch, word)
        st = _st()
        assert _drain(_done_check.gate(st, {"text": "All five errors are fixed; make passes."}))[1] is None, word
        assert st.convo == []


def test_a_model_that_reasons_first_is_read_at_its_last_word(monkeypatch):
    _model(monkeypatch, "The reply is not DONE because errors remain.\n\nUNFINISHED")
    assert _drain(_done_check.gate(_st(), {"text": "Two errors left."}))[1] == "continue"


def test_a_failed_call_never_holds_the_answer(monkeypatch):
    _model(monkeypatch, RuntimeError("timeout"))
    assert _drain(_done_check.gate(_st(), {"text": "Two errors left."}))[1] is None


def test_it_does_not_ask_where_it_does_not_apply(monkeypatch):
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    text = {"text": "Two errors left."}
    assert _drain(_done_check.gate(_st(plan_mode=True), text))[1] is None
    assert _drain(_done_check.gate(_st(readonly_mode=True), text))[1] is None
    assert _drain(_done_check.gate(_st(session_id=None), text))[1] is None
    assert _drain(_done_check.gate(_st(wrapping_up=True), text))[1] is None
    assert _drain(_done_check.gate(_st(), text, "skill"))[1] is None
    assert _drain(_done_check.gate(_st(), text, None, True))[1] is None
    monkeypatch.setenv("AIFORGE_CHAT_DONE_CHECK", "0")
    assert _drain(_done_check.gate(_st(), text))[1] is None
    assert seen == []


def test_send_backs_and_calls_are_bounded(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _finish
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    st = _st()
    sigs = [_drain(_done_check.gate(st, {"text": "Two errors left."}))[1] for _ in range(10)]
    assert sigs.count("continue") == _done_check._SEND_BACKS     # no action in between
    assert len(seen) == 10                                       # each reply still read (for the note)
    sent = _done_check._SEND_BACKS
    for i in range(60):
        st.action_counts[f"read|{i}"] = 1
        sent += _drain(_done_check.gate(st, {"text": "Two errors left."}))[1] == "continue"
    assert sent == _finish._SEND_BACKS_PER_TURN
    assert len(seen) == _done_check._CALLS_PER_TURN


def test_a_reply_to_a_harness_check_is_not_asked_about(monkeypatch):
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    assert _drain(_done_check.gate(_st(), {"text": "SAME"}))[1] is None
    assert seen == []


def test_the_running_job_note_holds_only_while_the_command_runs(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    reply = {"text": "bg-7 is still running; the two fixes are committed."}
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [object()])
    assert _drain(_done_check.gate(_st(running_job_nudged=True), reply))[1] is None
    assert seen == []
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [])            # it ended since
    assert _drain(_done_check.gate(_st(running_job_nudged=True), reply))[1] == "continue"


def test_a_question_that_asks_leave_to_go_on_is_checked_too(monkeypatch):
    """"Do you want me to fix the remaining ones?" used to end the turn unread."""
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    st = _st()
    reply = "Two of the six errors are fixed. Do you want me to fix the remaining ones?"
    assert _drain(_done_check.gate(st, {"text": reply}))[1] == "continue"
    assert "leave to do what the request already asks" in seen[0][1][0]["content"]
    assert "Do not ask for leave" in st.convo[-1]["content"]
    _model(monkeypatch, "NEEDS_USER")
    assert _drain(_done_check.gate(_st(), {"text": "Which of the two VMs should I deploy to?"}))[1] is None


def test_the_check_reads_what_the_harness_measured(monkeypatch):
    """Work over ssh changes no local file: only the commands show whether
    the fix was run after the last change."""
    from aiforge_core.runtime import action_log
    steps = [
        {"type": "tool", "name": "file_patch", "args": {"path": "engine/src/ch_read.rs"}, "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "ssh vm 'cd engine && cargo build'"},
         "result": {"ok": False, "code": 101, "stderr": "error[E0308]: mismatched types"}},
    ]
    monkeypatch.setattr(action_log, "live_steps", lambda sid: steps)
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    st = _st(board={"part-2": {"title": "fix errors one by one", "status": "pending"}})
    assert _drain(_done_check.gate(st, {"text": "Fixed the type error in ch_read.rs."}))[1] == "continue"
    asked = seen[0][1][1]["content"]
    assert "MEASURED BY THE HARNESS" in asked and "✗ run_command" in asked and "cargo build" in asked
    assert "FAILED AFTER THE LAST FILE CHANGE" in asked
    assert "TASK BOARD, STILL OPEN: part-2: fix errors one by one" in asked
    assert "no check, test or run of it after the last change" in seen[0][1][0]["content"]


def test_a_bare_continue_is_judged_against_the_request_before_it(monkeypatch):
    seen = []
    _model(monkeypatch, "DONE", seen)
    st = _st(goal="continue", convo=[
        {"role": "system", "content": "s"},
        {"role": "user", "content": "fix the six build errors on the VM one by one"},
        {"role": "assistant", "content": "Two fixed."},
        {"role": "user", "content": "OBSERVATION: exit 1"},
        {"role": "user", "content": "continue"}])
    _drain(_done_check.gate(st, {"text": "All six are fixed; the build passes."}))
    asked = seen[0][1][1]["content"]
    assert "THE REQUEST BEFORE IT" in asked and "six build errors" in asked and "OBSERVATION" not in asked


def test_an_answer_the_check_could_not_send_back_again_says_so(monkeypatch):
    _model(monkeypatch, "UNFINISHED")
    st = _st()
    for _ in range(_done_check._SEND_BACKS):
        assert _drain(_done_check.gate(st, {"text": "Two errors left."}))[1] == "continue"
        assert _done_check.unfinished_note(st) == ""
    assert _drain(_done_check.gate(st, {"text": "Two errors left."}))[1] is None
    assert "does not look finished" in _done_check.unfinished_note(st)
    _model(monkeypatch, "DONE")                 # the next reply is read for itself
    assert _drain(_done_check.gate(st, {"text": "All six fixed; the build passes."}))[1] is None
    assert _done_check.unfinished_note(st) == ""
    _model(monkeypatch, "UNFINISHED")
    _drain(_done_check.gate(st, {"text": "Two errors left."}))
    assert _done_check.unfinished_note(st) != ""
    _drain(_done_check.gate(st, {"text": "SAME"}))               # not a reply to judge: no stale note
    assert _done_check.unfinished_note(st) == ""
    done = _st()
    _model(monkeypatch, "DONE")
    _drain(_done_check.gate(done, {"text": "All fixed; make passes."}))
    assert _done_check.unfinished_note(done) == ""


def test_open_task_list_items_are_named_in_the_answer():
    from aiforge_core.runtime.chat_agent._turn import _finish
    st = SimpleNamespace(board={"a": {"title": "commit to the branch", "status": "done"},
                                "b": {"title": "fix the errors one by one", "status": "pending"}},
                         board_used=True, board_touched=True, readonly_mode=False)
    assert "fix the errors one by one" in _finish._open_items_note(st, None)
    assert "commit to the branch" not in _finish._open_items_note(st, None)
    assert _finish._open_items_note(st, None, "Which branch should I push to?") == ""
    st.plan_mode = True
    assert _finish._open_items_note(st, None) == ""
    st.plan_mode = False
    st.board["b"]["status"] = "done"
    assert _finish._open_items_note(st, None) == ""


def test_a_copy_of_the_harness_lines_is_taken_out_of_an_answer():
    from aiforge_core.runtime.chat_agent._turn import _finish
    st = SimpleNamespace(done_check_unfinished=True)
    copied = ("All fixed; the build passes." + _done_check.unfinished_note(st)
              + "\n\n_Not done from the task list: fix the errors one by one._")
    assert _finish._strip_own_notes(copied) == "All fixed; the build passes."
    assert _finish._strip_own_notes("Plain answer.\n\n_emphasis kept_") == "Plain answer.\n\n_emphasis kept_"


def test_a_command_that_ends_differently_is_progress():
    """A remote build that gets past one error to the next moves, though
    nothing changes on this machine."""
    from aiforge_core.runtime.chat_agent._turn import _idle_steps
    st = SimpleNamespace()
    _idle_steps._fields(st)
    args = {"cmd": "ssh vm 'cd engine && cargo build'"}

    def run(ok, err=""):
        return _idle_steps._outcome_moved(st, "run_command", args, {"ok": ok, "code": 0 if ok else 101, "stderr": err})
    a = "error[E0308]: mismatched types\n --> src/ch_read.rs:12:5"
    b = "error[E0425]: cannot find value `rows` in this scope\n --> src/ch_read.rs:40:9"
    assert run(False, a) is False            # the first failure is where it starts
    assert run(False, a) is False            # the same error again
    assert run(False, b) is True             # past it, onto the next one
    assert run(False, a) is False            # back to one it has shown before
    assert run(True) is True                 # it passes now
    assert run(True) is False
    for i in range(40):                      # a script that dies differently each time
        run(False, f"error[E{1000 + i}]: thing number {i} went wrong\n --> src/x{i}.rs:1:1")
    assert st.np_outcomes[next(iter(st.np_outcomes))]["new"] == _idle_steps._NEW_ERRORS_COUNTED
    flaps = sum(run(ok, a) for ok in [True, False] * 10)
    assert flaps <= _idle_steps._PASSES_COUNTED
    assert _idle_steps._outcome_moved(st, "run_command", args, {"ok": False, "running": True}) is False


def test_the_no_progress_note_does_not_send_the_model_to_the_user():
    from aiforge_core.runtime import no_progress
    text = no_progress.nudge_text("12 similar steps")
    assert "ask the user" not in text and "carry on" in text


def test_what_the_user_typed_later_goes_to_the_check(monkeypatch):
    seen = []
    _model(monkeypatch, "DONE", seen)
    st = _st(steers=["stop fixing, just tell me the cause of the first error"])
    assert _drain(_done_check.gate(st, {"text": "The cause is a missing import."}))[1] is None
    assert "just tell me the cause" in seen[0][1][1]["content"]
    assert "override" in seen[0][1][1]["content"]


def test_stop_ends_the_wait(monkeypatch):
    import threading
    import time
    from aiforge_core.llm import client
    from aiforge_core.runtime import chat_cancel
    gate = threading.Event()
    monkeypatch.setattr(client, "complete", lambda *a, **k: gate.wait(30) and "UNFINISHED")
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    t = time.monotonic()
    assert _done_check.verdict("chat", "goal", "reply", "x", session_id=7) == ""
    assert time.monotonic() - t < 5
    gate.set()


def test_final_nudges_ask_the_check(monkeypatch):
    from aiforge_core.runtime import cmd_jobs
    from aiforge_core.runtime.chat_agent._turn import _finish
    monkeypatch.setattr(cmd_jobs, "turn_running", lambda: [])
    _model(monkeypatch, "UNFINISHED")
    st = _st(builder_finalized=False, builder_final_tries=0, continue_nudges=0,
             board={}, board_used=False, readonly_mode=False, plan_mode=False)
    step = {"text": "Two of the five errors are fixed; three are left."}
    events, sig = _drain(_finish._final_nudges(st, step, None, False, []))
    assert sig == "continue" and st.convo[-1]["content"] == _done_check.NUDGE


# ── the statement before the first change ────────────────────────────────────

def test_a_statement_that_closes_its_last_line_with_proceed_goes_on():
    """Live 10-08: "… just say so — otherwise, PROCEED" was taken as the answer
    and the turn ended on the plan."""
    st = SimpleNamespace(convo=[], plan_pending=True)
    text = ("I will fix `add` in calc.py and add test_calc.py.\n"
            "If you'd rather I not touch anything, just say so — otherwise, PROCEED")
    events, sig = _drain(_plan_first.on_text(st, {"text": text}))
    assert sig == "continue" and st.convo[-1]["content"] == _plan_first.GO
    shown = events[0]["text"]
    assert shown.endswith("otherwise") and "PROCEED" not in shown


def test_a_reply_without_the_word_is_still_the_answer():
    for text in ("Nothing needs to change: calc.py already adds. I can proceed if you want.",
                 "Tell me which side should change and I will PROCEED.",
                 "Awaiting your go-ahead to PROCEED"):
        st = SimpleNamespace(convo=[], plan_pending=True)
        assert _drain(_plan_first.on_text(st, {"text": text}))[1] is None, text
