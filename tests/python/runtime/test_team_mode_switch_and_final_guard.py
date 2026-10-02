"""A chat switched from simple to Team, and a change request that ends without a
change.

Live report: the user chatted in simple mode (history and a finished task board
stayed), switched the same window to Team and asked for an implementation. The
run had "a plan" and stopped. Three guards:

* the first Team turn of a chat is a fresh request for routing and is never
  downgraded to the single agent just because the history looks like a
  follow-up;
* the planner-facing prompt tells the pipeline that earlier "done" claims are
  unverified and that the request must be executed;
* the single chat agent cannot end a change request with a plan: a zero-edit
  FINAL gets one reminder (bigger tasks) and is always labelled.
"""
from __future__ import annotations

import types

import pytest

from aiforge_core.api.routes._chat import _routing as R
from aiforge_core.runtime import chat_pipeline_prompt as PP
from aiforge_core.runtime.chat_agent._turn import _finish

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


def test_the_first_team_turn_is_not_downgraded(monkeypatch):
    from aiforge_core.runtime import turn_router
    monkeypatch.setattr(turn_router, "classify", lambda *a, **k: "simple")
    monkeypatch.delenv("AIFORGE_TEAM_AUTO_ROUTE", raising=False)
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "~80% built"}]
    # An established team chat still downgrades a small follow-up...
    assert R._maybe_downgrade_team(True, ASK, hist, "/r", 1) == (False, True)
    # ...but the first Team turn after a simple chat keeps the pipeline.
    assert R._maybe_downgrade_team(True, ASK, hist, "/r", 1,
                                   first_team_turn=True) == (True, False)


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
    monkeypatch.setattr(_finish, "_worktree_fingerprint", lambda cwd: "fp")


def _guard(st, step, asks=(), plan=False, readonly=False):
    return _run(_finish._no_change_guard(st, step, "/r", readonly, "", plan,
                                         list(asks), "fp"))


def test_a_plan_for_a_bigger_change_request_is_sent_back_once(clean_tree):
    st, step = _st(), {"text": "Bottom line: the real work is (a), (b), (c)."}
    events, sig = _guard(st, step, asks=["part one", "part two"])
    assert sig == "continue"
    assert "NO file" in st.convo[-1]["content"]
    assert any("no file has been edited" in e.get("text", "") for e in events)
    # The reminder is spent: the next zero-edit final is accepted, labelled.
    events, sig = _guard(st, step, asks=["part one", "part two"])
    assert sig is None
    assert step["text"].startswith("(No file was changed")
    assert "Bottom line" in step["text"]


def test_a_long_request_is_bigger_even_without_parts(clean_tree):
    st = _st(goal=ASK + " " + "x" * 200)
    _, sig = _guard(st, {"text": "plan"})
    assert sig == "continue"


def test_a_small_task_is_labelled_without_another_model_turn(clean_tree):
    st = _st(goal="fix the typo in a.py")
    step = {"text": "The typo is on line 3."}
    _, sig = _guard(st, step)
    assert sig is None and st.convo == []
    assert step["text"].startswith("(No file was changed")


def test_a_turn_that_edited_is_left_alone(clean_tree):
    step = {"text": "done"}
    assert _guard(_st(edits=2), step)[1] is None
    assert step["text"] == "done"


def test_a_change_on_disk_without_a_counted_edit_is_left_alone(monkeypatch):
    monkeypatch.setattr(_finish, "_worktree_fingerprint", lambda cwd: "other")
    step = {"text": "done"}
    assert _guard(_st(), step)[1] is None and step["text"] == "done"


def test_no_git_signal_means_no_verdict(monkeypatch):
    monkeypatch.setattr(_finish, "_worktree_fingerprint", lambda cwd: "")
    step = {"text": "done"}
    assert _guard(_st(), step)[1] is None and step["text"] == "done"


@pytest.mark.parametrize("kw", [{"plan": True}, {"readonly": True}])
def test_plan_and_read_only_modes_are_untouched(clean_tree, kw):
    step = {"text": "the plan"}
    assert _guard(_st(), step, **kw)[1] is None and step["text"] == "the plan"


def test_a_question_or_a_non_change_request_is_untouched(clean_tree):
    step = {"text": "Which database should it use?"}
    assert _guard(_st(), step)[1] is None and step["text"].startswith("Which")
    step = {"text": "it works like this"}
    assert _guard(_st(goal="explain the read path"), step)[1] is None
    assert step["text"] == "it works like this"


def test_the_guard_can_be_switched_off(clean_tree, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_NO_CHANGE_GUARD", "0")
    step = {"text": "a plan"}
    assert _guard(_st(), step)[1] is None and step["text"] == "a plan"
