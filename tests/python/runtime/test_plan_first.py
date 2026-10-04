"""The first change of a turn comes with a statement for the user."""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _plan_first as pf

LONG = ("You want the code to follow the Confluence page. I will change "
        "billing/calc.py and leave the page untouched. Steps: read, patch, test.")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SAY_PLAN", "1")


def _st(**kw):
    base = dict(session_id=3, readonly_mode=False, builder="", strict_finish=False,
                goal="make sure the code is according to the confluence page",
                convo=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _run(st, name, args=None, thought=""):
    gen = pf.gate(st, {"thought": thought}, name, args or {})
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def _text(st, text):
    step = {"text": text}
    gen = pf.on_text(st, step)
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events, step


def test_a_silent_first_write_is_held_and_the_statement_is_shown_before_the_work():
    st = _st()
    sig, events = _run(st, "file_patch", {"path": "a.py"})
    assert sig == "continue" and st.convo[-1]["content"] == pf.ASK
    assert events[0]["role"] == "system" and st.plan_pending
    sig, events, _step = _text(st, LONG + "\n\nPROCEED")
    assert sig == "continue"
    assert events == [{"type": "message", "supplementary": True, "role": "plan",
                       "text": LONG}]
    assert st.convo[-2] == {"role": "assistant", "content": LONG}
    assert st.convo[-1]["content"] == pf.GO
    # the changes of the turn then run without being held
    assert _run(st, "file_patch", {"path": "a.py"}) == (None, [])
    assert _text(st, "Done.")[0] is None


def test_an_answer_instead_of_the_statement_ends_the_turn_with_no_change():
    st = _st(goal="check what needs to be done")
    _run(st, "file_write", {"path": "a.py"})
    answer = "Three things are missing: rounding, the empty list, negatives."
    sig, events, step = _text(st, answer)
    assert sig is None and events == [] and step["text"] == answer
    assert st.zero_edit_checked and st.zero_edit_answer == answer
    assert not st.plan_pending


@pytest.mark.parametrize("tail", ["PROCEED", "**PROCEED**", "proceed.", "PROCEED\n\n"])
def test_the_closing_word_is_read_in_the_forms_a_model_writes(tail):
    st = _st()
    _run(st, "file_write", {"path": "a.py"})
    assert _text(st, LONG + "\n" + tail)[0] == "continue"


def test_a_text_reply_outside_the_ask_is_left_alone():
    st = _st()
    assert _text(st, "All done.\nPROCEED")[0] is None and not st.convo


def test_a_statement_made_next_to_the_call_costs_no_extra_step():
    st = _st()
    sig, events = _run(st, "confluence_update", {"id": "1"}, LONG)
    assert sig is None and events[0]["text"] == LONG and not st.convo


def test_a_model_that_calls_again_without_a_word_is_not_asked_twice():
    st = _st()
    assert _run(st, "file_write", {"path": "a.py"})[0] == "continue"
    assert _run(st, "file_write", {"path": "a.py"}) == (None, [])
    assert len(st.convo) == 1


@pytest.mark.parametrize("name,args", [
    ("file_read", {"path": "a.py"}), ("run_command", {"cmd": "pytest"}),
    ("grep", {"pattern": "x"}), ("editor", {"command": "view", "path": "a.py"}),
    ("confluence_read", {"id": "1"}),
])
def test_reads_and_commands_are_never_held(name, args):
    assert _run(_st(), name, args) == (None, [])


@pytest.mark.parametrize("name", ["file_write", "confluence_update", "jira_create",
                                  "gitlab_mr_create", "workflow_run"])
def test_what_counts_as_a_change(name):
    assert pf.changes_something(name, {"path": "a"})


@pytest.mark.parametrize("kw", [
    {"session_id": None}, {"readonly_mode": True}, {"builder": "job"},
    {"strict_finish": True}, {"goal": "yes continue"},
])
def test_who_is_not_asked(kw):
    assert _run(_st(**kw), "file_write", {"path": "a.py"}) == (None, [])


def test_the_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SAY_PLAN", "0")
    assert _run(_st(), "file_write", {"path": "a.py"}) == (None, [])


def test_the_note_covers_check_requests_and_the_direction_of_a_change():
    assert "check, review, compare, list" in pf.ASK
    assert "changes the code, not the page" in pf.ASK and "no tool call" in pf.ASK
