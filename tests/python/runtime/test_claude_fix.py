"""Unit tests for claude_fix targeted-fix pass."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import claude_fix


class _StubTicket:
    def __init__(self):
        self.identifier = "ONE-99"
        self.title = "Pin jar version"
        self.body = "README has 1.1.4; should be 1.2.16"
        self.project = "TallyConnector"
        self.metadata = None


def test_validator_reject_extracts_verdict() -> None:
    should, reason = claude_fix._validator_reject({
        "verdict": "request_changes", "rationale": "scope drift",
    })
    assert should is True
    assert reason == "scope drift"


def test_validator_reject_passes_through_string_json() -> None:
    payload = json.dumps({
        "verdict": "abstain", "rationale": "not enough context",
    })
    should, reason = claude_fix._validator_reject(payload)
    assert should is True
    assert "context" in reason


def test_validator_reject_returns_false_on_approve() -> None:
    should, _ = claude_fix._validator_reject({"verdict": "approve"})
    assert should is False


def test_validator_reject_handles_empty() -> None:
    assert claude_fix._validator_reject(None) == (False, "")
    assert claude_fix._validator_reject({}) == (False, "")


def test_build_fix_prompt_carries_each_signal() -> None:
    t = _StubTicket()
    prompt = claude_fix._build_fix_prompt(
        ticket=t, enhanced_body="goal: pin 1.2.16",
        file_diffs=[{"path": "README.md", "diff": "@@ -1 +1 @@\n- old\n+ new\n"}],
        feedback_verdict="pass",
        validator_rationale="missed line 81",
        memory_md="## Memory hits\n- prior pin shipped",
    )
    assert "ONE-99" in prompt
    assert "missed line 81" in prompt
    assert "README.md" in prompt
    assert "prior pin shipped" in prompt
    assert "scope_allowlist_globs" in prompt
    # finishing rules visible
    assert "run_tests" in prompt
    assert "finish" in prompt


def test_attempt_fix_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CLAUDE_FIX", "0")
    out = claude_fix.attempt_fix(
        ticket=_StubTicket(),
        pipeline_state={"validator_verdict": {"verdict": "request_changes"}},
        memory_md="",
        runner_module=MagicMock(),
        skip_researcher=True,
    )
    assert out["ok"] is False
    assert out["attempted"] is False
    assert out["reason"] == "disabled"


def test_attempt_fix_skips_when_validator_approved(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CLAUDE_FIX", "1")
    out = claude_fix.attempt_fix(
        ticket=_StubTicket(),
        pipeline_state={"validator_verdict": {"verdict": "approve"}},
        memory_md="",
        runner_module=MagicMock(),
        skip_researcher=True,
    )
    assert out["attempted"] is False
    assert out["reason"] == "validator_did_not_reject"


def test_attempt_fix_calls_runner_pipeline_and_persists_recipe(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CLAUDE_FIX", "1")

    fake_runner = MagicMock()
    fake_runner._run_pipeline = MagicMock(
        # Return state where the second-pass Validator now approves.
        return_value={
            "validator_verdict": {"verdict": "approve",
                                  "rationale": "fix landed"},
            "file_diffs": [{"path": "README.md", "diff": "patch"}],
        },
    )

    persist_calls = {}

    def _fake_persist(*, facts, repo, ticket_identifier=None, **_kw):
        persist_calls["facts"] = facts
        persist_calls["repo"] = repo
        return {"written_decisions": 1, "written_observations": 0,
                "errors": []}

    fake_lp = MagicMock(persist_facts=_fake_persist)

    # ``asyncio.run`` calls the coroutine; fake_runner._run_pipeline is
    # a plain MagicMock so wrap it.
    monkeypatch.setattr(claude_fix.asyncio, "run",
                        lambda coro: fake_runner._run_pipeline())

    with patch.dict("sys.modules", {
        "aiforge_core.runtime.learner_persist": fake_lp,
    }):
        out = claude_fix.attempt_fix(
            ticket=_StubTicket(),
            pipeline_state={
                "validator_verdict": {
                    "verdict": "request_changes",
                    "rationale": "missed line 81 in README",
                },
                "enhanced_body": "goal: pin 1.2.16 everywhere",
                "file_diffs": [{"path": "README.md", "diff": "first attempt"}],
                "feedback_verdict": "pass",
            },
            memory_md="",
            runner_module=fake_runner,
            skip_researcher=True,
        )

    assert out["ok"] is True
    assert out["attempted"] is True
    assert out["new_verdict"] == "approve"
    # ``asyncio.run`` shim above calls ``_run_pipeline()`` itself, and
    # the production code path also invokes it once — total 2.
    assert fake_runner._run_pipeline.call_count >= 1
    # Recipe persisted as a DECISION: fact so the Learner path routes
    # it to Decision_v2 rather than Observation_v2.
    assert persist_calls
    assert persist_calls["facts"][0]["text"].startswith("DECISION:")
    assert persist_calls["repo"] == "TallyConnector"
