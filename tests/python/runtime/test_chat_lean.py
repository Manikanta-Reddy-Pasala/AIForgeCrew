"""Lean Simple chat: the steps that only re-ask the model are off; the ones
that run code, guard the work and keep the panels are on."""
from __future__ import annotations

import os

import pytest

from aiforge_core.runtime.chat_agent import lean


@pytest.fixture(autouse=True)
def _restore_env():
    # lean.apply() uses os.environ.setdefault, which monkeypatch does not
    # track: put the environment back so later tests see the full flow
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_lean_sets_its_switches_but_an_explicit_setting_wins(monkeypatch):
    for k in lean.LEAN_OFF:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "1")
    monkeypatch.setenv("AIFORGE_CHAT_SPEC_AUDIT", "1")       # the operator's choice
    lean.apply()
    import os
    assert os.environ["AIFORGE_CHAT_DONE_CHECK"] == "0"
    assert os.environ["AIFORGE_CHAT_REVIEW_PASS"] == "0"
    assert os.environ["AIFORGE_CHAT_SAY_PLAN"] == "0"
    assert os.environ["AIFORGE_CHAT_SPEC_AUDIT"] == "1"


def test_lean_off_changes_nothing(monkeypatch):
    for k in lean.LEAN_OFF:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "0")
    lean.apply()
    import os
    assert not any(k in os.environ for k in lean.LEAN_OFF)


def test_the_steps_read_the_switches(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_check, _plan_first, _spec_audit
    monkeypatch.setenv("AIFORGE_CHAT_DONE_CHECK", "0")
    monkeypatch.setenv("AIFORGE_CHAT_SAY_PLAN", "0")
    monkeypatch.setenv("AIFORGE_CHAT_SPEC_AUDIT", "0")
    assert not _done_check.enabled() and not _plan_first.enabled() and not _spec_audit._on()


def test_lean_chat_sends_the_users_words_without_a_restatement(monkeypatch):
    from aiforge_core.api.routes._chat import _stages
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "1")
    called = []

    class _PP:
        @staticmethod
        def _enhance(*a, **k):
            called.append(1)
            return "restated"
    assert _stages._enhance_prompt(_PP, "build x", [], ".", False) == "build x"
    assert not called


def test_what_lean_keeps_stays_on(monkeypatch):
    for k in lean.LEAN_OFF:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_LEAN", "1")
    lean.apply()
    from aiforge_core.runtime.chat_agent._turn import _example_gate, _spec_run, _trace_hint
    assert _spec_run._on() and _trace_hint._on()
    import os
    for kept in ("AIFORGE_CHAT_SPEC_RUN", "AIFORGE_CHAT_VERIFY_ON_FINAL", "AIFORGE_CHAT_PROBE_GUARD",
                 "AIFORGE_CHAT_JOB_WAIT_MAX_S", "AIFORGE_CHAT_GOALS"):
        assert kept not in lean.LEAN_OFF
