"""Unit tests for the Claude-Enhance → local-Doer → Claude-Validate
pattern, now wired through proper ADK LlmAgents (2026-05-23 refactor).

Covers:
* pipeline.build_pipeline — Enhancer at index 0, Validator at end
* lm_health.check_lm_health — both endpoints OK / fail / restart
* failure_memory.record_failure + after-callback shape
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import failure_memory, lm_health, pipeline


# ── Pipeline shape ─────────────────────────────────────────────────────

def test_pipeline_starts_with_enhancer_and_ends_with_live_verifier() -> None:
    p = pipeline.build_pipeline(skip_researcher=True)
    names = [s.name for s in (getattr(p, "sub_agents", []) or [])]
    # enhancer must be the FIRST stage so the rewritten body reaches
    # the Planner / Doer.
    assert names[0] == "enhancer", names
    # live_verifier is now the tail so its veto sees Validator's
    # approval AND the final worktree state before git_pr commits.
    assert names[-1] == "live_verifier", names
    # Validator still runs immediately before live_verifier.
    assert names[-2] == "validator", names
    # Loop wrapper still lives between the planner stages and learner.
    assert "doer_refiner_feedback_loop" in names


def test_pipeline_with_researcher_keeps_order() -> None:
    p = pipeline.build_pipeline(skip_researcher=False)
    names = [s.name for s in (getattr(p, "sub_agents", []) or [])]
    assert names[0] == "enhancer", names
    assert "researcher" in names
    assert names[-1] == "live_verifier", names
    assert names[-2] == "validator", names


def test_pipeline_can_disable_live_verifier(monkeypatch) -> None:
    """AIFORGE_LIVE_VERIFIER=0 falls back to Validator-as-tail for
    operators who don't want the live boot stage."""
    monkeypatch.setenv("AIFORGE_LIVE_VERIFIER", "0")
    p = pipeline.build_pipeline(skip_researcher=True)
    names = [s.name for s in (getattr(p, "sub_agents", []) or [])]
    assert names[-1] == "validator", names
    assert "live_verifier" not in names


# ── LM health ────────────────────────────────────────────────────────────

def test_lm_health_both_endpoints_ok(monkeypatch) -> None:
    monkeypatch.setattr(lm_health, "_probe", lambda url, timeout=3.0: True)
    monkeypatch.setattr(lm_health, "_restart_tunnel", lambda u: True)
    out = lm_health.check_lm_health()
    assert out["ok"] is True
    assert out["doer_ok"] is True
    assert out["planner_ok"] is True
    assert out["restarted"] == []


def test_lm_health_doer_down_triggers_restart(monkeypatch) -> None:
    probe_calls = {"count": 0}

    def _probe(url, timeout=3.0):
        probe_calls["count"] += 1
        # First two probes (doer first call + planner first call) fail;
        # post-restart probes return True so the function logs the
        # recovery path.
        return probe_calls["count"] > 2

    monkeypatch.setattr(lm_health, "_probe", _probe)
    monkeypatch.setattr(lm_health, "_restart_tunnel", lambda u: True)
    out = lm_health.check_lm_health(restart_on_fail=True)
    assert "doer" in out["restarted"]
    assert "planner" in out["restarted"]
    assert out["doer_ok"] is True
    assert out["planner_ok"] is True


def test_lm_health_no_restart_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(lm_health, "_probe", lambda url, timeout=3.0: False)
    monkeypatch.setattr(
        lm_health, "_restart_tunnel",
        lambda u: pytest.fail("must not be called"),
    )
    out = lm_health.check_lm_health(restart_on_fail=False)
    assert out["ok"] is False
    assert out["restarted"] == []


# ── Failure memory ───────────────────────────────────────────────────────

class _StubTicket:
    def __init__(self, body="goal: do X", title="T", project="repo"):
        self.title = title
        self.body = body
        self.project = project
        self.metadata = None
        self.identifier = "ONE-99"


def test_failure_memory_skips_on_pass() -> None:
    out = failure_memory.record_failure(_StubTicket(), verdict="pass")
    assert out["ok"] is False
    assert out["error"] == "not_a_failure"


def test_failure_memory_skips_without_project() -> None:
    out = failure_memory.record_failure(
        _StubTicket(project=""), verdict="fail",
    )
    assert out["ok"] is False
    assert out["error"] == "no_project"


def test_failure_memory_calls_upsert(monkeypatch) -> None:
    t = _StubTicket()
    fake_upsert = MagicMock(
        return_value={"id": "obs_xyz", "deduped": False},
    )
    fake_store = MagicMock(upsert_observation=fake_upsert)
    fake_gdb = MagicMock()
    fake_gdb.driver.return_value = MagicMock()
    with patch.dict("sys.modules", {
        "aiforge_memory.features.memory.store": fake_store,
        "neo4j": MagicMock(GraphDatabase=fake_gdb),
    }):
        out = failure_memory.record_failure(
            t, verdict="fail", reason="something broke",
            ci_status="red",
        )
    assert out["ok"] is True
    fake_upsert.assert_called_once()
    kwargs = fake_upsert.call_args.kwargs
    assert kwargs["kind"] == "failure"
    assert "kind:failure" in kwargs["tags"]
    assert "ci:red" in kwargs["tags"]


def test_failure_memory_callback_skips_on_clean_pass(monkeypatch) -> None:
    """Both feedback=pass AND validator approve → no write."""
    cb = failure_memory.make_failure_memory_after_callback()
    state = {
        "ticket_identifier": "ONE-1",
        "ticket_project": "TestRepo",
        "feedback_verdict": "pass",
        "validator_verdict": json.dumps({
            "verdict": "approve", "rationale": "looks good",
        }),
    }
    called = {"n": 0}
    monkeypatch.setattr(
        failure_memory, "record_failure",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state
    import asyncio
    asyncio.run(cb(callback_context=ctx))
    assert called["n"] == 0


def test_failure_memory_callback_writes_on_validator_reject(monkeypatch) -> None:
    """feedback=pass but validator request_changes → write."""
    cb = failure_memory.make_failure_memory_after_callback()
    state = {
        "ticket_identifier": "ONE-2",
        "ticket_project": "TestRepo",
        "feedback_verdict": "pass",
        "validator_verdict": json.dumps({
            "verdict": "request_changes",
            "rationale": "scope drift",
        }),
    }
    captured = {}

    def _rec(t, *, verdict, reason, review_verdict=None, **_kw):
        captured["verdict"] = verdict
        captured["reason"] = reason
        captured["review_verdict"] = review_verdict
        return {"ok": True}

    monkeypatch.setattr(failure_memory, "record_failure", _rec)

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state
    import asyncio
    asyncio.run(cb(callback_context=ctx))
    assert captured.get("review_verdict") == "request_changes"
    assert "scope drift" in (captured.get("reason") or "")
