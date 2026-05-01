"""Unit tests for Doer, Validator, Tester, Architect, Learner."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents import registry


# ─────────── Doer ─────────────────────────────────────────────────────

def test_doer_skips_when_no_write_step() -> None:
    d = registry.build("doer")
    out = d.run(ctx={"plan": {"steps": [{"action": "read"}]}})
    assert out["skipped"] is True
    assert out["reason"] == "no_write_step"


def test_doer_calls_llm_and_runs_detectors(tmp_path) -> None:
    # Fake target file
    (tmp_path / "src" / "X.java").parent.mkdir(parents=True)
    (tmp_path / "src" / "X.java").write_text("class X {}\n")

    fake_diff = (
        "```diff\n"
        "--- a/src/X.java\n"
        "+++ b/src/X.java\n"
        "@@ -1,1 +1,2 @@\n"
        " class X {}\n"
        "+// added\n"
        "```\n"
    )
    d = registry.build("doer")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_text",
        return_value=fake_diff,
    ):
        out = d.run(ctx={
            "plan": {"steps": [{
                "id": 1, "action": "edit", "target": "src/X.java",
            }]},
            "repo_path": str(tmp_path),
            "ticket_id": "TKT-TEST",
            "repo": "x",
        })
    assert out["artifact_type"] == "doer_outcome"
    assert out["target"] == "src/X.java"
    assert "udiff" in out
    assert isinstance(out["problems"], list)


# ─────────── Validator ────────────────────────────────────────────────

def test_validator_approves_clean_diff() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {
        "udiff": "diff content",
        "problems": [],
    }})
    assert out["decision"] == "approve"


def test_validator_blocks_on_hallucinated_import() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {
        "udiff": "diff content",
        "problems": [{"mode": "F-001", "evidence": "com.bogus"}],
    }})
    assert out["decision"] == "block"
    assert "no_hallucinated_imports" in out["reason"]


def test_validator_skip_when_doer_skipped() -> None:
    v = registry.build("validator")
    out = v.run(ctx={"doer_outcome": {"skipped": True, "reason": "x"}})
    assert out["decision"] == "skip"


# ─────────── Tester ───────────────────────────────────────────────────

def test_tester_returns_test_specs() -> None:
    fake = {
        "tests": [
            {"name": "t1", "target_class": "X", "target_method": "foo",
             "scenario": "happy", "expected": "ok", "framework": "junit5"},
        ],
        "coverage_target": 0.9,
    }
    t = registry.build("tester")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=fake,
    ):
        out = t.run(ctx={"understanding": {}, "plan": {}})
    assert len(out["tests"]) == 1
    assert out["coverage_target"] == 0.9


def test_tester_handles_invalid_json() -> None:
    t = registry.build("tester")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=None,
    ):
        out = t.run(ctx={})
    assert out["tests"] == []
    assert out["error"] == "llm_invalid_json"


# ─────────── Architect ────────────────────────────────────────────────

def test_architect_request_changes_when_validation_blocked() -> None:
    a = registry.build("architect")
    out = a.run(ctx={
        "validation": {"decision": "block", "reason": "missing_imports"},
        "doer_outcome": {"udiff": "x"},
    })
    assert out["decision"] == "request_changes"
    assert "validation blocked" in out["comments"][0]


def test_architect_calls_llm_when_validated() -> None:
    fake = {
        "decision": "approve",
        "comments": ["lgtm"],
        "mr_title": "feat: pagination",
        "mr_body": "## Summary\nadd page+size",
    }
    a = registry.build("architect")
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=fake,
    ):
        out = a.run(ctx={
            "validation": {"decision": "approve"},
            "doer_outcome": {"udiff": "diff"},
            "understanding": {}, "plan": {},
        })
    assert out["decision"] == "approve"
    assert out["mr_title"] == "feat: pagination"


# ─────────── Learner ──────────────────────────────────────────────────

def test_learner_writes_episodic_and_procedural() -> None:
    L = registry.build("learner")
    with patch(
        "aiforge_core.aiforge_agents.learner.online.record_episodic",
    ) as rec_e, patch(
        "aiforge_core.aiforge_agents.learner.online.update_procedural",
    ) as upd_p:
        out = L.run(ctx={
            "ticket_id": "TKT-X",
            "repo": "r",
            "plan": {"steps": [{"action": "read"}, {"action": "edit"}]},
            "verifier_verdict": {"verdict": "pass"},
            "grounding": {"resolved": True, "unresolved_refs": []},
            "doer_outcome": {"target": "src/main/feature/X.java",
                             "problems": []},
            "validation": {"decision": "approve"},
            "review": {"decision": "approve"},
        })
    assert out["outcome"] == "success"
    assert out["task_class"] == "feature"
    assert out["tool_sequence"] == ["read", "edit"]
    rec_e.assert_called_once()
    upd_p.assert_called_once()


def test_learner_outcome_blocked_when_grounding_fails() -> None:
    L = registry.build("learner")
    with patch(
        "aiforge_core.aiforge_agents.learner.online.record_episodic",
    ), patch(
        "aiforge_core.aiforge_agents.learner.online.update_procedural",
    ):
        out = L.run(ctx={
            "ticket_id": "TKT-Y", "repo": "r",
            "plan": {"steps": [{"action": "edit"}]},
            "verifier_verdict": {"verdict": "pass"},
            "grounding": {"resolved": False,
                          "unresolved_refs": [{"target": "x"}]},
            "doer_outcome": {"target": "a/b/c.java", "problems": []},
            "validation": {"decision": "skip"},
        })
    assert out["outcome"] == "blocked"
