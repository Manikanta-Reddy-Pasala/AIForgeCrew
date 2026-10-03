"""An answer that recounts EARLIER edits is not turned into a denial."""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._guards import file_edit
from aiforge_core.runtime.chat_agent._guards.base import run_guards
from aiforge_core.runtime.chat_agent._guards.file_edit import (
    RECAP_CHECK, FileEditClaimGuard, chat_wrote_before)

CLAIM = "Yes. I updated calc.py and added tests/test_calc.py."


def _st(session_id=7):
    return SimpleNamespace(convo=[], edits_made=0, edit_claim_nudges=0,
                           session_id=session_id, goal="ok continue with the rest")


def _run(st, text, monkeypatch, wrote=True):
    monkeypatch.setattr(file_edit, "chat_wrote_before", lambda _sid: wrote)
    monkeypatch.setattr(file_edit, "_worktree_fingerprint", lambda _c: " M x\n")
    step = {"text": text}
    gen = run_guards(st, step, [FileEditClaimGuard("/repo", False, "", " M x\n")])
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, step, events


def test_a_recap_is_asked_about_once_and_same_keeps_the_answer(monkeypatch):
    st = _st()
    sig, _step, _ev = _run(st, CLAIM, monkeypatch)
    sent = st.convo[-1]["content"]
    assert sig == "continue" and '"ok continue with the rest"' in sent
    assert "SAME" in sent and st.edit_claim_nudges == 0
    assert st.zero_edit_checked and st.zero_edit_answer == CLAIM
    sig, step, _ev = _run(st, "SAME", monkeypatch)
    assert sig is None and step["text"] == CLAIM


def test_a_corrected_answer_after_the_check_is_sent_without_a_label(monkeypatch):
    st = _st()
    _run(st, CLAIM, monkeypatch)
    sig, step, _ev = _run(st, "I updated calc.py earlier.", monkeypatch)
    assert sig is None and step["text"] == "I updated calc.py earlier."
    assert len(st.convo) == 1


def test_a_chat_that_never_wrote_keeps_the_firm_nudge(monkeypatch):
    st = _st()
    sig, _step, _ev = _run(st, CLAIM, monkeypatch, wrote=False)
    assert sig == "continue" and st.edit_claim_nudges == 1
    assert "NO file-write" in st.convo[-1]["content"]


def test_the_check_tells_the_model_not_to_deny_or_ask_for_a_go_ahead():
    assert "SAME" in RECAP_CHECK and "go-ahead" in RECAP_CHECK
    assert "do it now with tool calls" in RECAP_CHECK


def test_a_reply_about_the_harness_note_is_not_sent_to_the_user(monkeypatch):
    from aiforge_core.runtime.chat_agent._guards.zero_edit import ZeroEditGuard
    st = _st()
    st.action_counts = {}
    st.head0 = None
    _run(st, CLAIM, monkeypatch)
    step = {"text": "No tools to run: the action log shows nothing and the note is a recap."}
    guard = ZeroEditGuard("/repo", False, "", False, [], " M x\n")
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent._guards.zero_edit._worktree_fingerprint",
        lambda _c: " M x\n")
    gen = guard.check(st, step)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        assert stop.value is None
    assert step["text"] == CLAIM


@pytest.mark.parametrize("steps,expected", [
    ([{"name": "file_read", "args": {}, "result": {"ok": True}}], False),
    ([{"name": "file_write", "args": {"path": "a"}, "result": {"ok": True}}], True),
    ([{"name": "file_write", "args": {"path": "a"}, "result": {"ok": False}}], False),
    ([{"name": "session_actions", "args": {}, "result": {"ok": True}}], False),
])
def test_chat_wrote_before_reads_the_stored_actions(monkeypatch, steps, expected):
    from aiforge_core.runtime import action_log
    monkeypatch.setattr(action_log, "_stored_steps", lambda _sid: steps)
    assert chat_wrote_before(3) is expected
    assert chat_wrote_before(None) is False


def test_a_default_model_without_a_provider_reaches_every_role(monkeypatch):
    from aiforge_core.config.agent_config import _resolve
    monkeypatch.delenv("AIFORGE_DEFAULT_PROVIDER", raising=False)
    monkeypatch.setenv("AIFORGE_DEFAULT_MODEL", "some/model")
    row = _resolve._env_default_row()
    assert row["provider"] == "openai_compatible" and row["model"] == "some/model"
    monkeypatch.delenv("AIFORGE_DEFAULT_MODEL")
    assert _resolve._env_default_row() is None
