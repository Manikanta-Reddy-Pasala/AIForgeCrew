"""Unit tests for the Claude-Enhance → local-Doer → Claude-Review
pattern shipped 2026-05-23.

Covers:
* enhancer.enhance — prompt build, blocked-reason path, success path
* lm_health.check_lm_health — both endpoints OK / fail / restart
* failure_memory.record_failure — soft-fail on missing project
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import enhancer, failure_memory, lm_health


# ── Enhancer ────────────────────────────────────────────────────────────

class _StubTicket:
    def __init__(self, body="goal: do X", title="T", project="repo"):
        self.title = title
        self.body = body
        self.project = project
        self.metadata = None


def test_enhancer_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ENHANCER", "0")
    out = enhancer.enhance(_StubTicket())
    assert out["ok"] is False
    assert out["error"] == "disabled"


def test_enhancer_returns_ok_when_cli_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ENHANCER", "1")
    monkeypatch.setattr(
        enhancer, "_claude_cli_invoke",
        lambda prompt, **kw: "# Enhanced\n## Goal\ndo X\n",
    )
    out = enhancer.enhance(_StubTicket())
    assert out["ok"] is True
    assert out["used_claude"] is True
    assert "Goal" in out["enhanced_body"]


def test_enhancer_blocked(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ENHANCER", "1")
    monkeypatch.setattr(
        enhancer, "_claude_cli_invoke",
        lambda prompt, **kw: "ENHANCE_BLOCKED: body is empty",
    )
    out = enhancer.enhance(_StubTicket(body=""))
    assert out["ok"] is False
    assert out["blocked_reason"] == "body is empty"
    assert out["used_claude"] is True


def test_enhancer_cli_failure_is_soft(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ENHANCER", "1")
    monkeypatch.setattr(enhancer, "_claude_cli_invoke",
                        lambda prompt, **kw: None)
    out = enhancer.enhance(_StubTicket())
    assert out["ok"] is False
    assert out["error"] == "claude_unreachable_or_failed"
    assert out["used_claude"] is False


def test_enhancer_strips_code_fences(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ENHANCER", "1")
    monkeypatch.setattr(
        enhancer, "_claude_cli_invoke",
        lambda prompt, **kw: "```markdown\n# Hi\n## Goal\ngo\n```",
    )
    out = enhancer.enhance(_StubTicket())
    assert out["ok"] is True
    assert not out["enhanced_body"].startswith("```")
    assert not out["enhanced_body"].endswith("```")


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

def test_failure_memory_skips_on_pass() -> None:
    t = _StubTicket()
    out = failure_memory.record_failure(t, verdict="pass")
    assert out["ok"] is False
    assert out["error"] == "not_a_failure"


def test_failure_memory_skips_without_project() -> None:
    t = _StubTicket(project="")
    out = failure_memory.record_failure(t, verdict="fail")
    assert out["ok"] is False
    assert out["error"] == "no_project"


def test_failure_memory_calls_upsert(monkeypatch) -> None:
    t = _StubTicket()
    t.identifier = "ONE-99"
    fake_upsert = MagicMock(
        return_value={"id": "obs_xyz", "deduped": False},
    )
    fake_store = MagicMock(upsert_observation=fake_upsert)
    fake_driver = MagicMock()
    fake_gdb = MagicMock()
    fake_gdb.driver.return_value = fake_driver
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
