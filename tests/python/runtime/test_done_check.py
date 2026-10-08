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
    assert _drain(_done_check.gate(_st(), text, "skill"))[1] is None
    assert _drain(_done_check.gate(_st(), text, None, True))[1] is None
    assert _drain(_done_check.gate(_st(), {"text": "Which branch should I push to?"}))[1] is None
    monkeypatch.setenv("AIFORGE_CHAT_DONE_CHECK", "0")
    assert _drain(_done_check.gate(_st(), text))[1] is None
    assert seen == []


def test_send_backs_and_calls_are_bounded(monkeypatch):
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    st = _st()
    sigs = [_drain(_done_check.gate(st, {"text": "Two errors left."}))[1] for _ in range(10)]
    assert sigs.count("continue") == _done_check._SEND_BACKS     # no action in between
    assert len(seen) == _done_check._SEND_BACKS                  # no call it could not use
    for i in range(20):
        st.action_counts[f"read|{i}"] = 1
        _drain(_done_check.gate(st, {"text": "Two errors left."}))
    assert len(seen) == _done_check._CALLS_PER_TURN


def test_a_reply_to_a_harness_check_and_a_running_job_answer_are_not_asked_about(monkeypatch):
    seen = []
    _model(monkeypatch, "UNFINISHED", seen)
    assert _drain(_done_check.gate(_st(), {"text": "SAME"}))[1] is None
    st = _st(running_job_nudged=True)
    assert _drain(_done_check.gate(st, {"text": "bg-7 is still running; the two fixes are committed."}))[1] is None
    assert seen == []


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
