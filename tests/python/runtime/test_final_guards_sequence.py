"""The FINAL guards, pinned through the two entry points the loop uses.

``_final_nudges`` runs the action-log echo sanitiser and the external-system
claim guard (then the builder / implicit-final / running-job / task-board
nudges); ``_handle_final`` then runs the file-edit claim guard and the
zero-edit change guard. Each one nudges a bounded number of times and then
labels the answer. These tests pin the exact text, the counters and the ORDER,
so the guards can be moved without changing what the model or the user sees.
"""
from __future__ import annotations

import types

import pytest

from aiforge_core.runtime.chat_agent._turn import _finish


def _st(**kw):
    base = dict(convo=[], board_used=False, board={}, edits_made=0,
                readonly_mode=False, continue_nudges=0, action_counts={},
                edit_claim_nudges=0, verify_rounds=99, goal="the retry settings",
                no_change_nudges=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _drive(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _nudges(st, step, builder=""):
    return _drive(_finish._final_nudges(st, step, builder, False, []))


@pytest.fixture
def final(monkeypatch):
    monkeypatch.setattr(_finish, "_fire_stop", lambda *a, **k: None)
    monkeypatch.setattr(_finish, "_endpoint_one_slot", lambda: True)
    monkeypatch.setenv("AIFORGE_CHAT_NO_CHANGE_GUARD", "0")

    def run(st, step, builder=""):
        return _drive(_finish._handle_final(st, step, builder, False, False,
                                            False, "", [], ""))
    return run


# ── action-log echo ───────────────────────────────────────────────────────

_ECHO_THOUGHT = ("↺ the reply was only an action log — asking for the actual "
                 "result")


def test_an_action_log_only_reply_is_nudged_three_times_then_replaced():
    st = _st()
    for n in (1, 2, 3):
        step = {"text": "[did: file_read(a)✓]"}
        events, sig = _nudges(st, step)
        assert sig == "continue"
        assert step["text"] == ""
        assert events == [{"type": "thought", "role": "system",
                           "text": _ECHO_THOUGHT}]
        assert st.convo[-1]["content"].startswith(
            "[loop guard — not the user] Your reply was only a log")
        assert st.echo_nudges == n
    step = {"text": "[did: file_read(a)✓]"}
    events, sig = _nudges(st, step)
    assert sig is None and events == []
    assert step["text"].startswith("(I could not produce a result for this")


def test_a_log_after_real_content_is_stripped_without_a_nudge():
    st = _st()
    step = {"text": "The retry is capped at 30 minutes.\n[did: grep✓]"}
    events, sig = _nudges(st, step)
    assert sig is None and events == [] and st.convo == []
    assert step["text"] == "The retry is capped at 30 minutes."


# ── external-system claim ─────────────────────────────────────────────────

_CLAIM = "I created the Confluence page for the design."


def test_an_unbacked_external_claim_is_nudged_twice_then_labelled():
    st = _st()
    for n in (1, 2):
        step = {"text": _CLAIM}
        events, sig = _nudges(st, step)
        assert sig == "continue" and step["text"] == _CLAIM
        assert events == [{"type": "thought", "role": "system", "text":
                           "↺ the answer says something was created, but no "
                           "tool call for it succeeded — checking"}]
        assert st.convo[-1]["content"].startswith(
            "[loop guard — not the user] Your answer says something was "
            "created, pushed or updated in Confluence, but no Confluence tool")
        assert st.external_nudges == n
    step = {"text": _CLAIM}
    events, sig = _nudges(st, step)
    assert sig is None and events == []
    assert step["text"] == (
        "(Nothing was actually created or changed in Confluence in this turn "
        "— no tool call for it succeeded. Ask me to do it and I will, and "
        "report what the system returns.)\n\n" + _CLAIM)


def test_a_backed_external_claim_and_a_builder_session_are_left_alone():
    step = {"text": _CLAIM}
    assert _nudges(_st(external_ok={"confluence"}), step)[1] is None
    assert step["text"] == _CLAIM
    step = {"text": _CLAIM}
    assert _nudges(_st(builder_finalized=True), step, builder="skill")[1] is None
    assert step["text"] == _CLAIM


# ── file-edit claim ───────────────────────────────────────────────────────

_EDIT = "I updated config.yaml with the new timeout."


def test_an_unbacked_edit_claim_is_nudged_twice_then_labelled(final):
    st = _st()
    for n in (1, 2):
        step = {"type": "final", "text": _EDIT}
        events, sig = final(st, step)
        assert sig == "continue" and step["text"] == _EDIT
        assert events == [
            {"type": "thought", "text": _EDIT},
            {"type": "thought", "role": "system",
             "text": "⚠ you described file edits but no write ran and "
                     "nothing changed on disk — applying for real…"}]
        assert st.convo[-1]["content"].startswith(
            "[automated check — not the user] Your message claims you edited")
        assert st.edit_claim_nudges == n
    step = {"type": "final", "text": _EDIT}
    events, sig = final(st, step)
    assert sig == "return"
    assert events[0] == {"type": "message", "text":
                         "⚠ Note: no file changes were recorded this turn — "
                         "nothing was written to disk.\n\n" + _EDIT}
    assert events[-1] == {"type": "done"}
    assert st.edit_claim_nudges == 2


def test_same_after_the_edit_claim_nudge_sends_the_answer_that_was_written(final):
    """Live ("when did you commit"): the answer read as an edit claim, and the
    reply to the nudge replaced it, so the commit time never reached the user."""
    answer = ("The commit `c692e2f` was made at 20:39:30.\n"
              "- 20:37:30 — session start (worktree created; `stats.py` mtime)")
    st = _st()
    step = {"type": "final", "text": answer}
    assert final(st, step)[1] == "continue"
    assert st.convo[-1]["content"].endswith(
        "reply with the single word SAME: it is then sent as you wrote it.")
    events, sig = final(st, {"type": "final", "text": "SAME"})
    assert sig == "return"
    assert events[0] == {"type": "message", "text":
                         "⚠ Note: no file changes were recorded this turn — "
                         "nothing was written to disk.\n\n" + answer}
    assert st.edit_claim_nudges == 1


@pytest.mark.parametrize("question", [
    "when did you commit", "how would you add a multiply function?",
    "explain how it works"])
def test_the_answer_to_a_plain_question_is_not_checked_for_edit_claims(final, question):
    st = _st(goal=question)
    step = {"type": "final", "text": _EDIT}
    events, sig = final(st, step)
    assert sig == "return" and st.convo == [] and st.edit_claim_nudges == 0
    assert events[0] == {"type": "message", "text": _EDIT}


def test_a_question_that_asks_for_a_change_is_still_checked(final):
    st = _st(goal="can you update the timeout in config.yaml?")
    assert final(st, {"type": "final", "text": _EDIT})[1] == "continue"


def test_an_edit_claim_is_left_alone_in_read_only_mode(final):
    events, sig = _drive(_finish._handle_final(
        _st(readonly_mode=True), {"type": "final", "text": _EDIT}, "", False,
        False, True, "", [], ""))
    assert sig == "return"
    assert events[0]["text"] == _EDIT


# ── order ─────────────────────────────────────────────────────────────────

def test_the_external_claim_is_checked_before_the_edit_claim(final):
    both = _CLAIM + " I updated config.yaml."
    st = _st()
    step = {"type": "final", "text": both}
    final(st, step)
    final(st, step)
    assert st.convo[-1]["content"].startswith("[loop guard")      # external x2
    assert st.edit_claim_nudges == 0
    events, sig = final(st, step)
    # external is spent: its label goes on, then the edit claim is nudged
    assert sig == "continue"
    assert step["text"].startswith("(Nothing was actually created")
    assert st.convo[-1]["content"].startswith("[automated check")
    assert st.edit_claim_nudges == 1


def test_the_zero_edit_guard_runs_after_the_edit_claim_guard(monkeypatch):
    """A change request answered with a plan and no landed edit: the claim
    guard has nothing to say (the plan claims no edit), then the zero-edit
    guard labels it. (The tree has no git signal here, so it must stay quiet
    unless the fingerprint says "unchanged" — pinned in
    test_team_mode_switch_and_final_guard.)"""
    monkeypatch.setattr(_finish, "_fire_stop", lambda *a, **k: None)
    monkeypatch.setattr(_finish, "_endpoint_one_slot", lambda: True)
    st = _st(goal="please implement the retry cap in client.py")
    step = {"type": "final", "text": "The plan is to cap retries."}
    events, sig = _drive(_finish._handle_final(st, step, "", False, False,
                                               False, "", [], ""))
    assert sig == "return"
    assert events[0]["text"] == "The plan is to cap retries."
