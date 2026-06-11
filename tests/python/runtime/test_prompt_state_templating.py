"""Context reaches each stage even under compaction.

The Workflow runs agents as chat-mode nodes (history-based context),
but ``ContextFilterPlugin`` trims old invocations once a run exceeds
``keep`` (~12). The critical hand-offs are therefore ALSO injected from
``session.state`` via ADK's ``{key?}`` optional templating — state is
never compacted, so these survive. These tests verify the real ADK
``inject_session_state`` substitutes our keys and leaves the prompts'
own literal JSON braces untouched.
"""
from __future__ import annotations

import asyncio

from google.adk.utils.instructions_utils import inject_session_state

from aiforge_core.runtime import prompts


class _Sess:
    def __init__(self, st):
        self.state = st


class _IC:
    def __init__(self, st):
        self.session = _Sess(st)
        self.artifact_service = None


class _RO:
    def __init__(self, st):
        self._invocation_context = _IC(st)


def _render(template, state):
    return asyncio.run(inject_session_state(template, _RO(state)))


def test_doer_injects_plan_context_verdict_from_state() -> None:
    out = _render(prompts.DOER, {
        "plan_md": "STEP1 do x",
        "context_brief_md": "repo: foo.py",
        "verifier_verdict": {"verdict": "pass"},
        # replan_note absent
    })
    assert "STEP1 do x" in out
    assert "repo: foo.py" in out
    assert "'verdict': 'pass'" in out
    # missing optional key renders empty, never KeyErrors
    assert "{replan_note?}" not in out


def test_doer_own_json_braces_survive_templating() -> None:
    out = _render(prompts.DOER, {"plan_md": "p"})
    # the prompt's literal output-contract braces must NOT be eaten
    assert "{file_diffs:" in out


def test_validator_injects_plan_and_doer_outcome() -> None:
    out = _render(prompts.VALIDATOR, {
        "plan_md": "the plan",
        "doer_outcome": '{"file_diffs":[]}',
        "feedback_verdict": "pass",
    })
    assert "the plan" in out
    assert '{"file_diffs":[]}' in out  # JSON-string value survives
    assert "IN-LOOP FEEDBACK VERDICT:\npass" in out


def test_planner_injects_enhanced_body() -> None:
    out = _render(prompts.PLANNER, {"enhanced_body": "GOAL: add endpoint"})
    assert "GOAL: add endpoint" in out
    # planner's own subticket-shape braces survive
    assert '"slug"' in out


def test_missing_state_keys_render_blank_not_error() -> None:
    # empty state — every {key?} should collapse to '' without raising
    for tmpl in (prompts.DOER, prompts.VALIDATOR, prompts.PLANNER):
        out = _render(tmpl, {})
        assert "{enhanced_body?}" not in out
        assert "{plan_md?}" not in out
        assert "{doer_outcome?}" not in out
