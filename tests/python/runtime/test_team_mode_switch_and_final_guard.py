"""A chat switched from simple to Team, and a change request that ends without a
change.

Live report: the user chatted in simple mode (history and a finished task board
stayed), switched the same window to Team and asked for an implementation. The
run had "a plan" and stopped. Three guards:

* the first Team turn of a chat is a fresh request for routing, and a Team turn
  is never downgraded (to the single agent, or to the research agent) by a
  classifier: the mode is the user's pick;
* the planner-facing prompt tells the pipeline that earlier "done" claims are
  unverified and that the request must be executed;
* a single-agent turn that is about to end with no file changed asks the
  WORKING model once, in its own context, whether the user's message wanted
  work (the harness no longer decides that from the wording).
"""
from __future__ import annotations

import types

import pytest

from aiforge_core.api.routes._chat import _routing as R
from aiforge_core.runtime import chat_pipeline_prompt as PP
from aiforge_core.runtime.chat_agent._guards import ZeroEditGuard, run_guards, zero_edit

ASK = ("implement a rust/go based parallel read path, target 500 ms for 50k "
       "records")


# ─── the first Team turn of a chat ─────────────────────────────────────


def _row(i, role, mode):
    return {"id": i, "role": role, "mode": mode, "content": "x"}


def test_a_chat_that_never_ran_team_has_its_first_team_turn():
    rows = [_row(1, "user", "simple"), _row(2, "assistant", "simple"),
            _row(3, "user", "team")]            # the message just sent
    assert R._first_team_turn(rows, 3) is True


def test_a_chat_with_an_earlier_team_turn_does_not():
    rows = [_row(1, "user", "team"), _row(2, "assistant", "team"),
            _row(3, "user", "team")]
    assert R._first_team_turn(rows, 3) is False


@pytest.mark.parametrize("prompt", [ASK, "rename it", "yes continue", "thanks"])
def test_a_team_turn_is_never_downgraded_to_the_single_agent(monkeypatch, prompt):
    """Team is the user's pick for the message. No classifier (it was a cheap
    SIMPLE/COMPLEX call on a few truncated lines) overrules it, on the first
    Team turn or on a follow-up."""
    from aiforge_core.api.routes._chat import _producer
    from aiforge_core.llm import client
    from aiforge_core.runtime import chat_approve, turn_router

    def boom(*a, **k):
        raise AssertionError("a model was asked whether to run the team")
    monkeypatch.setattr(client, "complete", boom)
    assert not hasattr(R, "_maybe_downgrade_team")
    assert not hasattr(turn_router, "classify")
    assert not hasattr(turn_router, "should_downgrade_team")
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "~80% built"}]
    pc = types.SimpleNamespace(
        team=True, prompt=prompt, history=hist, cwd="/r", session_id=991,
        _auto_downgraded=False, agent_mode="act",
        body=types.SimpleNamespace(review_edits=False))
    _producer._prepare_turn(pc, chat_approve,
                            types.SimpleNamespace(enabled=lambda: False))
    assert pc.team is True and pc._auto_downgraded is False
    chat_approve.finish(991)


def _route(first_team_turn, history, monkeypatch, cat="doc_analysis"):
    from aiforge_core.config import approval_settings
    from aiforge_core.runtime import task_router
    monkeypatch.setattr(approval_settings, "required", lambda *a, **k: False)
    monkeypatch.setattr(task_router, "classify_task", lambda *a, **k: cat)
    pp = types.SimpleNamespace(enabled=lambda: True,
                               _is_greenfield=lambda cwd: False)
    return R._decide_chat_route(pp, ASK, "act", True, True, "/r", history,
                                first_team_turn=first_team_turn)


HIST = [{"role": "user", "content": "check the read path"},
        {"role": "assistant", "content": "~80% built; 2/2 tasks done"}]


def test_a_mode_switched_team_request_pipelines_and_is_classified(monkeypatch):
    rd = _route(True, HIST, monkeypatch)
    assert rd.cat == "doc_analysis"          # classified like a fresh turn...
    assert not rd.doc_task                   # ...and not sent to research
    assert rd.route_pipeline


def test_without_the_switch_a_followup_is_not_pipelined_in_parallel(monkeypatch):
    rd = _route(False, HIST, monkeypatch)
    assert not rd.doc_task and not rd.route_pipeline   # sequential team


# ─── what the pipeline is told ─────────────────────────────────────────


def test_a_change_request_gets_the_execute_directive_and_unverified_history(
        monkeypatch, tmp_path):
    from aiforge_core.runtime import context_bundle
    monkeypatch.setattr(context_bundle, "build_bundle",
                        lambda *a, **k: types.SimpleNamespace(
                            blocks=lambda: [], rules_md="", memory_md="",
                            preferences_md=""))
    prompt, _state = PP._build_team_prompt(
        str(tmp_path), ASK, HIST + [{"role": "user", "content": ASK}], None, "")
    assert PP.EXECUTE_DIRECTIVE in prompt
    assert "claims that work is done are unverified" in prompt
    assert "~80% built" in prompt                       # still context
    assert prompt.index("CURRENT REQUEST") < prompt.index(PP.EXECUTE_DIRECTIVE)


def test_a_question_gets_neither(monkeypatch, tmp_path):
    from aiforge_core.runtime import context_bundle
    monkeypatch.setattr(context_bundle, "build_bundle",
                        lambda *a, **k: types.SimpleNamespace(
                            blocks=lambda: [], rules_md="", memory_md="",
                            preferences_md=""))
    prompt, _ = PP._build_team_prompt(
        str(tmp_path), "how does the read path work?",
        HIST + [{"role": "user", "content": "how does the read path work?"}],
        None, "")
    assert PP.EXECUTE_DIRECTIVE not in prompt
    assert "continue with this context" in prompt


# ─── the single agent cannot finish a change request with a plan ───────


def _st(goal=ASK, edits=0, **kw):
    base = dict(convo=[], goal=goal, edits_made=edits, board_used=False,
                board={}, no_change_nudges=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


@pytest.fixture
def clean_tree(monkeypatch):
    """A git workspace whose tree did not change this turn."""
    monkeypatch.setattr(zero_edit, "_worktree_fingerprint", lambda cwd: "fp")


def _guard(st, step, asks=(), plan=False, readonly=False):
    return _run(run_guards(st, step, [ZeroEditGuard(
        "/r", readonly, "", plan, list(asks), "fp")]))


def test_a_zero_edit_final_is_put_to_the_working_model_once(clean_tree):
    """Whether the request wanted work is the model's call, made in its own
    context: one harness note, then its answer stands."""
    st, step = _st(), {"text": "Bottom line: the real work is (a), (b), (c)."}
    events, sig = _guard(st, step, asks=["part one", "part two"])
    assert sig == "continue"
    note = st.convo[-1]["content"]
    assert note == zero_edit.CHECK and note.startswith("[harness — not the user]")
    assert "No file was changed in this turn" in note
    assert "the user's last message, read together with the conversation" in note
    assert "reply with the single word SAME" in note
    assert events == [{"type": "thought", "role": "system",
                       "text": "↻ no file was changed — checking that against "
                               "what you asked…"}]
    # "SAME": the answer the model had already written is the turn's answer
    same = {"text": "SAME"}
    assert _guard(_st(zero_edit_checked=True, zero_edit_answer="It is 42."), same)[1] is None
    assert same["text"] == "It is 42."
    # Asked once. The model's next FINAL is its answer: accepted, not labelled.
    step = {"text": "Nothing needs to change: read.rs already does it (line 40)."}
    events, sig = _guard(st, step, asks=["part one", "part two"])
    assert sig is None and events == []
    assert step["text"].startswith("Nothing needs to change")
    assert len(st.convo) == 1


@pytest.mark.parametrize("goal", [ASK, ASK + " " + "x" * 200, "fix the typo in a.py",
                                  "same for the rest pls", "yes continue"])
def test_the_check_does_not_depend_on_the_wording_or_size_of_the_request(clean_tree, goal):
    st = _st(goal=goal)
    _, sig = _guard(st, {"text": "Here is the plan."})
    assert sig == "continue" and st.convo[-1]["content"] == zero_edit.CHECK


def test_an_answer_that_says_it_could_not_be_done_is_labelled(clean_tree):
    st = _st(zero_edit_checked=True)
    step = {"text": "NOT DONE: the repo has no rust toolchain, cargo is missing."}
    _, sig = _guard(st, step)
    assert sig is None
    assert step["text"] == zero_edit.DISCLAIMER + \
        "the repo has no rust toolchain, cargo is missing."


def test_a_model_that_still_admits_it_did_nothing_gets_the_firm_reminder(clean_tree):
    st = _st(goal="its ok continue simplfying all files .. use kiss",
             zero_edit_checked=True, reasoning_armed=False)
    stall = "Nothing was written this turn. I will do step 1 in the next turn."
    for n in (1, 2, 3):
        step = {"text": stall}
        events, sig = _guard(st, step)
        assert sig == "continue" and st.no_change_nudges == n
        assert "ALREADY told you to go ahead" in st.convo[-1]["content"]
    step = {"text": stall}
    _, sig = _guard(st, step)
    assert sig is None and step["text"] == zero_edit.DISCLAIMER + stall


def test_asking_again_for_a_go_ahead_that_was_given_is_a_stall(clean_tree):
    st = _st(goal="yes continue", zero_edit_checked=True)
    _, sig = _guard(st, {"text": "Shall I proceed with step 1?"})
    assert sig == "continue" and "ALREADY told you" in st.convo[-1]["content"]
    # …but asking is right when the user's message was not a go-ahead
    st = _st(goal="find out why the login page returns 500", zero_edit_checked=True)
    step = {"text": "It is the expiry compare in session.py. Shall I proceed with the fix?"}
    _, sig = _guard(st, step)
    assert sig is None and st.convo == [] and step["text"].startswith("It is the expiry")


def test_a_question_costs_no_extra_step(clean_tree):
    """The message reads as a question and names no change: the answer stands
    and no model call is spent on the check — whatever the model looked up to
    answer it (live, the note sent a model that had answered "tell me which
    functions calc.py defines" back to work for twenty tool calls)."""
    for goal in ("how does the read path work?", "what does main.rs do?",
                 "explain the read path", "tell me which functions calc.py defines"):
        for counts in ({}, {"file_read:x": 1}):
            st, step = _st(goal=goal, action_counts=counts), {"text": "It works like this."}
            events, sig = _guard(st, step)
            assert sig is None and events == [] and st.convo == [], goal
            assert step["text"] == "It works like this."
    # small talk, when no tool ran
    st = _st(goal="thanks", action_counts={})
    assert _guard(st, {"text": "You are welcome."})[1] is None and st.convo == []
    # a question that names a change, or is a go-ahead, is still put to the model
    for goal in ("can you fix the import in app.py?", "ok continue"):
        st = _st(goal=goal, action_counts={})
        assert _guard(st, {"text": "Here is what I would do."})[1] == "continue", goal


def test_nothing_was_changed_can_be_the_answer(clean_tree):
    """The firm reminder ("you were ALREADY told to go ahead") is never sent on
    the model's admission alone: the user's own words must say so."""
    st = _st(goal="last commit is 47 minutes ago, when did you commit",
             action_counts={"run_command:x": 1})
    assert _guard(st, {"text": "It was committed at 14:02."})[1] == "continue"   # the check
    step = {"text": "No changes were made in this turn; nothing was written. "
                    "The commit is from 14:02."}
    assert _guard(st, step)[1] is None                     # an answer, not a stall
    assert "ALREADY told you" not in st.convo[-1]["content"]
    assert "(No file was changed" not in step["text"]


def test_a_turn_that_edited_is_left_alone(clean_tree):
    step = {"text": "done"}
    assert _guard(_st(edits=2), step)[1] is None
    assert step["text"] == "done"


def test_a_change_on_disk_without_a_counted_edit_is_left_alone(monkeypatch):
    monkeypatch.setattr(zero_edit, "_worktree_fingerprint", lambda cwd: "other")
    step = {"text": "done"}
    assert _guard(_st(), step)[1] is None and step["text"] == "done"


def test_no_git_signal_means_no_verdict(monkeypatch):
    monkeypatch.setattr(zero_edit, "_worktree_fingerprint", lambda cwd: "")
    monkeypatch.setattr(zero_edit, "head_commit", lambda cwd: None)
    step = {"text": "done"}
    st = _st()
    assert _run(run_guards(st, step, [ZeroEditGuard("/r", False, "", False, [], "")]))[1] is None
    assert step["text"] == "done" and st.convo == []


def _clean_guard(st, step):
    return _run(run_guards(st, step, [ZeroEditGuard("/r", False, "", False, [], "")]))


def test_a_clean_tree_on_the_same_commit_is_an_unchanged_tree(monkeypatch):
    """A chat's workspace is committed at the start of every turn, so the tree
    is clean before and after a turn that did nothing. That is "unchanged",
    not "no signal": the check must still run."""
    monkeypatch.setattr(zero_edit, "_worktree_fingerprint", lambda cwd: "")
    monkeypatch.setattr(zero_edit, "head_commit", lambda cwd: "abc123")
    st = _st(head0="abc123")
    assert _clean_guard(st, {"text": "Here is the plan."})[1] == "continue"
    assert st.convo[-1]["content"] == zero_edit.CHECK


def test_work_that_was_committed_is_work(monkeypatch):
    """The model edited with a shell command and committed: the tree is clean
    again, but it is on a new commit."""
    monkeypatch.setattr(zero_edit, "_worktree_fingerprint", lambda cwd: "")
    monkeypatch.setattr(zero_edit, "head_commit", lambda cwd: "def456")
    st = _st(head0="abc123")
    step = {"text": "Committed the fix."}
    assert _clean_guard(st, step)[1] is None and st.convo == []


@pytest.mark.parametrize("kw", [{"plan": True}, {"readonly": True}])
def test_plan_and_read_only_modes_are_untouched(clean_tree, kw):
    step = {"text": "the plan"}
    assert _guard(_st(), step, **kw)[1] is None and step["text"] == "the plan"


def test_a_final_that_asks_the_user_something_is_untouched(clean_tree):
    step = {"text": "Which database should it use?"}
    st = _st()
    assert _guard(st, step)[1] is None and step["text"].startswith("Which")
    assert st.convo == []
    # …unless the user's message was a go-ahead: then the model is asked, in
    # context, whether it is asking for something it already has (no pattern
    # has to recognise the wording of "should I implement it now?").
    st = _st(goal="yes continue")
    assert _guard(st, {"text": "Should I implement it now?"})[1] == "continue"
    assert st.convo[-1]["content"] == zero_edit.CHECK


def test_the_guard_can_be_switched_off(clean_tree, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_NO_CHANGE_GUARD", "0")
    step = {"text": "a plan"}
    assert _guard(_st(), step)[1] is None and step["text"] == "a plan"
