"""A message typed while the agent works gets a reply the user can see, and a
typed ask for a plan is planned, not carried out."""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import chat_interject, chat_runs, chat_steer, plan_request
from aiforge_core.runtime.chat_agent._turn import _steer_reply as sr

_SID = 771_105
READ = 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STEER_REPLY", "1")
    monkeypatch.setenv("AIFORGE_CHAT_PLAN_REQUEST", "1")
    chat_interject.clear(_SID)
    yield
    chat_interject.clear(_SID)


def _st(**kw):
    base = dict(session_id=3, builder="", strict_finish=False, convo=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def _replies(events):
    return [e for e in events if e.get("type") == "message"
            and e.get("supplementary") and e.get("role") == "reply"]


# ── the gate ─────────────────────────────────────────────────────────────────

def test_no_message_no_gate():
    st = _st()
    assert _drive(sr.gate(st, {"thought": ""}, "file_read", {})) == (None, [])


def test_words_next_to_the_call_are_the_reply():
    st = _st()
    sr.on_drain(st, ["which port does it listen on?"])
    sig, events = _drive(sr.gate(
        st, {"thought": "It listens on 8080. Carrying on with the tests."},
        "file_read", {}))
    assert sig is None                                # the call goes on
    (reply,) = _replies(events)
    assert reply["text"].startswith("It listens on 8080")
    assert reply["to"] == "which port does it listen on?"
    assert st.reply_due == [] and st.convo == []


def test_a_silent_call_is_held_once_and_asked_for_the_reply():
    st = _st()
    sr.on_drain(st, ["also handle the empty case"])
    sig, events = _drive(sr.gate(st, {"thought": ""}, "file_write", {}))
    assert sig == "continue" and not _replies(events)
    assert st.convo[-1]["content"] == sr.ASK
    # the text that comes back is shown, and the work goes on
    sig, events = _drive(sr.on_text(st, {"text": "Yes — I will add that case."}))
    assert sig == "continue"
    assert _replies(events)[0]["text"] == "Yes — I will add that case."
    assert st.convo[-1]["content"] == sr.GO
    # the repeated call is not held again
    assert _drive(sr.gate(st, {"thought": ""}, "file_write", {})) == (None, [])


def test_a_model_that_stays_silent_is_not_asked_twice():
    """Live: asked which python runs the tests, the model answered the ask
    with the lookup. It is not held again, and what it writes next — here next
    to a later call — is the reply."""
    st = _st()
    sr.on_drain(st, ["why?"])
    assert _drive(sr.gate(st, {"thought": ""}, "file_read", {}))[0] == "continue"
    asks = len(st.convo)
    assert _drive(sr.gate(st, {"thought": ""}, "run_command", {})) == (None, [])
    assert _drive(sr.gate(st, {"thought": ""}, "run_command", {})) == (None, [])
    assert len(st.convo) == asks and st.reply_due == ["why?"]
    sig, events = _drive(sr.gate(
        st, {"thought": "Because the config pins it to 3.12."}, "editor", {}))
    assert sig is None and _replies(events)[0]["to"] == "why?"
    assert st.reply_due == []


def test_the_statement_before_a_first_change_is_the_reply():
    st = _st(plan_pending=True)
    sr.on_drain(st, ["name the file test_math.py"])
    sig, events = _drive(sr.on_text(
        st, {"text": "Understood, the test file is test_math.py.\nPROCEED"}))
    assert sig is None                    # the statement goes on to plan-first
    assert _replies(events)[0]["text"] == "Understood, the test file is test_math.py."


def test_the_turns_answer_after_the_message_is_the_reply():
    """No ask was sent: the text is the answer and ends the turn as usual."""
    st = _st()
    sr.on_drain(st, ["stop, just tell me what you found"])
    assert _drive(sr.on_text(st, {"text": "I found two bugs."})) == (None, [])
    assert st.reply_due == []


def test_work_producing_runs_and_the_switch():
    for st in (_st(builder="job"), _st(strict_finish=True), _st(session_id=None)):
        sr.on_drain(st, ["hello there"])
        assert not getattr(st, "reply_due", None)


def test_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STEER_REPLY", "0")
    st = _st()
    sr.on_drain(st, ["hello there"])
    assert not getattr(st, "reply_due", None)


def test_a_rejection_note_is_not_owed_a_reply():
    sr_st = _st()
    sr.on_drain(sr_st, [])
    assert not getattr(sr_st, "reply_due", None)


# ── through the loop ─────────────────────────────────────────────────────────

def test_a_mid_run_question_is_answered_and_the_task_goes_on(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    calls: list = []

    def fn(role, messages, **kw):
        calls.append(messages[-1].get("content"))
        n = len(calls)
        if n == 1:
            chat_interject.push(_SID, "is it still on python 3.11?")
            return READ
        if n == 2:
            return READ                      # read the message, said nothing
        if n == 3:
            return "Yes, still 3.11. Carrying on with the read."
        if n == 4:
            return READ
        return "all done"

    events = list(ca.run_chat_agent([{"role": "user", "content": "read a.txt"}],
                                    cwd=str(tmp_path), complete_fn=fn,
                                    session_id=_SID))
    (reply,) = _replies(events)
    assert reply["text"].startswith("Yes, still 3.11")
    assert reply["to"] == "is it still on python 3.11?"
    assert calls[2] == sr.ASK and calls[3] == sr.GO
    finals = [e for e in events if e.get("type") == "message"
              and not e.get("supplementary")]
    assert finals[-1]["text"] == "all done"
    assert [e for e in events if e.get("type") == "tool"
            and e.get("name") == "file_read"]


def test_a_final_after_the_message_costs_no_extra_step(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    calls: list = []

    def fn(role, messages, **kw):
        calls.append(1)
        if len(calls) == 1:
            chat_interject.push(_SID, "stop that, answer this instead")
            return READ
        return "here is the answer"

    events = list(ca.run_chat_agent([{"role": "user", "content": "read a.txt"}],
                                    cwd=str(tmp_path), complete_fn=fn,
                                    session_id=_SID))
    assert len(calls) == 2 and not _replies(events)


# ── the event, and what is said beside a run ─────────────────────────────────

def test_reply_event_shape():
    ev = chat_steer.reply_event(["use  postgres", "and add a test"], "Will do.")
    assert ev == {"type": "message", "supplementary": True, "role": "reply",
                  "text": "Will do.", "to": "use postgres / and add a test"}
    assert chat_steer.reply_event("one", "x")["to"] == "one"


def test_a_note_is_shown_live_and_kept_for_the_saved_turn():
    sid = 771_106
    run = chat_runs.start(sid)
    q = run.subscribe()
    ev = chat_steer.reply_event("status?", "**Status** — reading files")
    assert chat_runs.note(sid, ev) is True
    assert q.get(timeout=1) == ev
    assert chat_runs.take_notes(sid) == [ev]
    assert chat_runs.take_notes(sid) == []
    chat_runs.finish(sid)
    assert chat_runs.note(sid, ev) is False          # no run: nothing to show


def test_a_message_that_arrived_too_late_is_said_so():
    from aiforge_core.api.routes._chat._turn_events import _keep_side_replies
    sid = 771_107
    chat_interject.set_steerable(sid, True)
    chat_interject.push(sid, "one more thing")
    steps: list = []
    _keep_side_replies(sid, steps, cancelled=False)
    assert steps == [chat_steer.reply_event(["one more thing"],
                                            chat_steer.LATE_REPLY)]
    chat_interject.clear(sid)


# ── a typed ask for a plan ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "give me a plan for adding retries to the sync client",
    "plan how to move auth to JWT",
    "Please plan the migration first",
    "can you come up with an implementation plan for X",
    "just plan, don't implement yet",
    "I need a detailed plan to refactor the parser",
    "what's your plan for the cache layer?",
    "first plan it, I will review",
    "Plan only: rewrite of the stock service",
    "create a step-by-step plan for the upgrade",
])
def test_an_ask_for_a_plan(text):
    assert plan_request.asks_for_plan_only(text)


@pytest.mark.parametrize("text", [
    "Carry out the approved plan.\n\n1. plan the thing",
    "implement the plan",
    "fix the bug in parser.py",
    "plan and implement the retry logic",
    "add a pricing plan page",
    "no plan needed, just fix the test",
    "follow the plan above",
    "create the subscription plan model",
    "make a plan then implement it",
    "continue",
    "the plan looks good, go ahead",
    "update plan.md with the new steps",
    "don't plan, do it",
    "",
])
def test_not_an_ask_for_a_plan(text):
    assert not plan_request.asks_for_plan_only(text)


def test_only_the_opening_of_a_message_is_read():
    assert not plan_request.asks_for_plan_only(
        "fix the login bug. " + "x " * 300 + "give me a plan")


def test_plan_ask_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PLAN_REQUEST", "0")
    assert not plan_request.asks_for_plan_only("give me a plan for X")


def test_an_approved_plan_or_a_resume_is_never_a_plan_ask():
    from aiforge_core.api.routes._chat._message import _asks_for_plan
    body = SimpleNamespace(content="give me a plan for X", single_agent=False,
                           resume=None, builder=None)
    assert _asks_for_plan(body)
    for field, value in (("single_agent", True), ("resume", True),
                         ("builder", "job")):
        assert not _asks_for_plan(SimpleNamespace(**{**vars(body), field: value}))


# ── review round 1 ───────────────────────────────────────────────────────────

def test_one_ask_at_a_time_with_the_first_change_statement():
    """The plan-first statement is being asked for: the call is not held a
    second time for the reply, and the reply stays owed."""
    st = _st(plan_pending=True)
    sr.on_drain(st, ["use tmp/ for the output"])
    assert _drive(sr.gate(st, {"thought": ""}, "file_read", {})) == (None, [])
    assert st.reply_due == ["use tmp/ for the output"] and st.convo == []


def test_an_answer_without_proceed_is_the_answer_not_a_card():
    st = _st(plan_pending=True)
    sr.on_drain(st, ["what did you find?"])
    assert _drive(sr.on_text(st, {"text": "Two bugs, both in calc.py."})) == (None, [])


def test_a_final_after_an_ignored_ask_is_not_swallowed():
    """Asked for the reply, the model narrated ("On it.") and then finished:
    that FINAL is the turn's answer."""
    st = _st()
    sr.on_drain(st, ["also add a test"])
    assert _drive(sr.gate(st, {"thought": ""}, "file_write", {}))[0] == "continue"
    assert _drive(sr.on_narration(st, {"thought": "On it."})) == (None, [])
    assert _drive(sr.on_text(st, {"text": "Done: added the test."})) == (None, [])


def test_no_note_once_the_answer_is_out():
    sid = 771_108
    run = chat_runs.start(sid)
    run.publish({"type": "done"})
    assert chat_runs.note(sid, chat_steer.reply_event("status?", "x")) is False
    chat_runs.finish(sid)


def test_a_kept_note_is_not_published_twice():
    sid = 771_109
    run = chat_runs.start(sid)
    q = run.subscribe()
    ev = chat_steer.reply_event("x", chat_steer.TEAM_REPLY)
    assert chat_runs.note(sid, ev, publish=False) is True
    assert q.empty() and chat_runs.take_notes(sid) == [ev]
    chat_runs.finish(sid)


@pytest.mark.parametrize("text", [
    "show the current plan on the settings page",
    "create a new plan in Stripe",
    "plan and refactor the parser",
    "plan the migration then run it",
    "just plan on using the helper and add the endpoint",
    "plan and add the retry",
])
def test_build_requests_that_only_mention_a_plan(text):
    assert not plan_request.asks_for_plan_only(text)


def test_the_answer_to_the_plan_agents_question_is_still_the_plan(monkeypatch):
    from aiforge_core.api.routes._chat import _message
    from aiforge_core.runtime import chat_store
    body = SimpleNamespace(content="use Postgres", single_agent=False,
                           resume=None, builder=None)
    rows = [{"role": "user", "content": "give me a plan for X", "mode": "plan"},
            {"role": "assistant", "content": "Which database?",
             "steps": [{"type": "awaiting", "awaiting_input": True}]}]
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: rows)
    assert _message._answers_plan_question(1, body)
    rows[1]["steps"] = []                              # the plan was given
    assert not _message._answers_plan_question(1, body)
    rows[1]["steps"] = [{"type": "awaiting", "awaiting_input": True}]
    rows[0]["mode"] = "simple"                         # an act turn asked
    assert not _message._answers_plan_question(1, body)


# ── review round 2 ───────────────────────────────────────────────────────────

def test_words_shown_as_the_reply_are_not_asked_for_again_as_the_statement():
    st = _st()
    sr.on_drain(st, ["use tmp/"])
    _drive(sr.gate(st, {"thought": "Understood, writing to tmp/ from here on."},
                   "file_write", {}))
    assert st.plan_said is True


def test_a_reply_that_was_the_whole_answer_stays_the_answer():
    """Asked for the reply, the model wrote its full answer; told to carry on
    it ended with a stub. No tool ran in between: the answer is the reply."""
    st = _st(action_counts={"file_read": 2})
    sr.on_drain(st, ["stop and tell me what you found"])
    assert _drive(sr.gate(st, {"thought": ""}, "file_read", {}))[0] == "continue"
    full = "I found two bugs: add() subtracts, and total() skips the last item."
    assert _drive(sr.on_text(st, {"text": full}))[0] == "continue"
    step = {"text": "See above."}
    assert _drive(sr.on_text(st, step)) == (None, [])
    assert step["text"] == full
    # a tool ran in between: the new final stands
    st2 = _st(action_counts={"file_read": 2})
    sr.on_drain(st2, ["and then?"])
    _drive(sr.gate(st2, {"thought": ""}, "file_read", {}))
    _drive(sr.on_text(st2, {"text": "I will add the missing test after this read."}))
    st2.action_counts["file_read"] = 3
    step = {"text": "Done."}
    _drive(sr.on_text(st2, step))
    assert step["text"] == "Done."


@pytest.mark.parametrize("text", [
    "don't give me a plan, just fix it",
    "I don't want a plan",
    "Plan mode is broken, fix it",
    "write the plan to PLAN.md",
    "plan & implement the retry",
    "lets " * 60 + "go",
])
def test_more_messages_that_are_not_a_plan_ask(text):
    assert not plan_request.asks_for_plan_only(text)


def test_a_new_plan_is_a_plan_ask():
    assert plan_request.asks_for_plan_only("give me a new plan for the cache")


def test_skip_the_plan_ends_the_plan_question(monkeypatch):
    from aiforge_core.api.routes._chat import _message
    from aiforge_core.runtime import chat_store
    rows = [{"role": "user", "content": "give me a plan for X", "mode": "plan"},
            {"role": "assistant", "content": "Which database?",
             "steps": [{"type": "awaiting", "awaiting_input": True}]}]
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: rows)
    body = SimpleNamespace(content="skip the plan, just fix it",
                           single_agent=False, resume=None, builder=None)
    assert not _message._answers_plan_question(1, body)


def test_a_note_does_not_change_what_the_run_says_it_is_doing():
    sid = 771_110
    run = chat_runs.start(sid)
    run.phase = "running pytest"
    chat_runs.note(sid, chat_steer.reply_event("status?", "x"))
    assert run.phase == "running pytest"
    chat_runs.finish(sid)
