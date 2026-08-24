"""Crew consolidation hook: gating + trajectory build (gaps #1,2,5)."""
from __future__ import annotations

from aiforge_core.runtime import memory_consolidate as mc


def test_passed_detects_pass_and_approve():
    assert mc._passed({"feedback_verdict": "pass"})
    assert mc._passed({"validator_verdict": {"verdict": "approve"}})
    assert not mc._passed({"feedback_verdict": "fail"})
    assert not mc._passed({})


def test_trajectory_text_builds_from_state():
    t = mc._trajectory_text({
        "enhanced_body": "do X", "plan_md": "step 1",
        "doer_outcome": "diff", "feedback_verdict": "pass"})
    assert "TICKET" in t
    assert "do X" in t
    assert "PLAN" in t
    assert "step 1" in t
    assert mc._trajectory_text({}) == ""


def test_run_consolidation_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_CONSOLIDATE_DISABLE", "1")
    assert mc.run_consolidation({"feedback_verdict": "pass"}) == {
        "skipped": "disabled"}


def test_run_consolidation_skips_non_pass(monkeypatch):
    monkeypatch.delenv("AIFORGE_MEMORY_CONSOLIDATE_DISABLE", raising=False)
    assert mc.run_consolidation({"feedback_verdict": "fail"})["skipped"] \
        == "not a passing run"


def test_run_consolidation_skips_no_repo(monkeypatch):
    monkeypatch.delenv("AIFORGE_MEMORY_CONSOLIDATE_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    assert mc.run_consolidation({"feedback_verdict": "pass"})["skipped"] \
        == "no repo"
