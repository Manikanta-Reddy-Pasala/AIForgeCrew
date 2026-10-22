"""Scope net, part 3 — prompt stance, enhancer trim, session rules.

The prompts say "do only what the request says"; the enhancer restatement
may clarify but never add steps; rules captured for THIS chat reach the
rules block the model reads."""
import pytest
from aiforge_core.api.routes._chat import _stages
from aiforge_core.runtime.chat_agent import _native_prompt, _prompt_text
from aiforge_core.runtime.chat_agent._turn import _plan_llm


def test_prompts_say_do_only_the_request():
    for src in (_native_prompt.__dict__, _prompt_text.__dict__):
        text = " ".join(v for v in src.values() if isinstance(v, str))
        assert "state the assumption and continue" not in text
        assert "Suggested, not done" in text
    assert "Suggested, not done" in _plan_llm._RULES


def test_enhancer_keeps_clarifications_and_drops_added_steps():
    m = {"role": "user", "content": "fix the login bug in auth.py"}
    enriched = ("Fix the login bug in auth.py (the token expiry check). "
                "Add unit tests for the auth module. Refactor the session store. "
                "Run the existing tests.")
    _stages._augment_user_turn(m, enriched, "", "fix the login bug in auth.py", None)
    assert "token expiry" in m["content"]
    assert "Add unit tests" not in m["content"] and "Refactor the session" not in m["content"]
    assert "Run the existing tests" in m["content"]          # 'run' is not an added-work verb


def test_enhancer_old_behaviour_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ENHANCE_ADDS", "1")
    m = {"role": "user", "content": "fix the bug"}
    _stages._augment_user_turn(m, "Fix the bug. Add tests.", "", "fix the bug", None)
    assert "Add tests" in m["content"]


def test_session_rules_reach_the_rules_block(monkeypatch):
    from aiforge_core.runtime.rule_capture import _base, _store
    from aiforge_core.runtime.chat_agent._tools import _memory as M
    monkeypatch.setattr(M, "_md_rule_bullets", lambda cwd: [])
    monkeypatch.setattr(M, "_repo_rule_bullets", lambda cwd: [])
    monkeypatch.setattr(M, "_constraint_bullets", lambda cwd: [])
    _base._SESSION_ITEMS["42"] = [{"category": "rule", "scope": "session", "canonical": "for this chat, never touch migrations"}]
    try:
        out = M._rules_context("/r", "anything", session_id=42)
        assert "never touch migrations" in out and out.startswith("RULES")
        assert "never touch migrations" in " ".join(M.user_rule_lines("/r", session_id=42))
        assert M._rules_context("/r", "anything", session_id=43) == ""
    finally:
        _base._SESSION_ITEMS.pop("42", None)
