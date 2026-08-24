"""Tests for the Workflow parallel stages + merge nodes
(runtime.parallel_stages)."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import parallel_stages as ps


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


# ── merge_context ───────────────────────────────────────────────────────

def test_merge_context_concats_present_briefs() -> None:
    state = {
        "research_brief_md": "found foo.py",
        "memory_brief_md": "past failure X",
        "repo_brief_md": "bar.py::Baz",
        # conventions missing
    }
    _run(ps.merge_context(_FakeCtx(state)))
    merged = state["context_brief_md"]
    assert "## Researcher" in merged
    assert "found foo.py" in merged
    assert "## Repo map" in merged
    # memory is injected directly via {memory_brief_md?} (trivial path
    # skips this merge node) — never folded here, or it would double.
    assert "## Memory" not in merged
    assert "## Conventions" not in merged


def test_merge_context_no_briefs_is_noop() -> None:
    state: dict = {}
    _run(ps.merge_context(_FakeCtx(state)))
    assert "context_brief_md" not in state


def test_merge_context_skips_blank() -> None:
    state = {"research_brief_md": "   ", "repo_brief_md": "real"}
    _run(ps.merge_context(_FakeCtx(state)))
    assert "## Researcher" not in state["context_brief_md"]
    assert "## Repo map" in state["context_brief_md"]


def test_merge_context_caps_each_section(monkeypatch) -> None:
    # Fix C1b: a runaway gatherer output is capped at its SOURCE before merge.
    monkeypatch.setenv("AIFORGE_CTX_SECTION_CHARS", "2000")
    state = {"research_brief_md": "R" * 50000, "repo_brief_md": "K" * 50000}
    _run(ps.merge_context(_FakeCtx(state)))
    merged = state["context_brief_md"]
    # Each section bounded (2000 + marker), so the merged brief stays small.
    assert len(merged) < 2 * 2000 + 500
    assert "truncated to fit context" in merged


def test_merge_context_section_cap_env_off(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CTX_SECTION_CHARS", "0")   # disabled
    state = {"repo_brief_md": "K" * 5000}
    _run(ps.merge_context(_FakeCtx(state)))
    assert len(state["context_brief_md"]) > 5000          # uncapped


# ── merge_verdicts ───────────────────────────────────────────────────────

def test_merge_verdicts_all_pass() -> None:
    state = {
        "verify_correctness": '{"verdict": "pass"}',
        "verify_scope": '{"verdict": "pass"}',
        "verify_risk": {"verdict": "pass"},
    }
    _run(ps.merge_verdicts(_FakeCtx(state)))
    assert state["verifier_verdict"]["verdict"] == "pass"


def test_merge_verdicts_any_reject_rejects() -> None:
    state = {
        "verify_correctness": '{"verdict": "pass"}',
        "verify_scope": '{"verdict": "reject", "rationale": "empty globs"}',
        "verify_risk": '{"verdict": "pass"}',
    }
    _run(ps.merge_verdicts(_FakeCtx(state)))
    v = state["verifier_verdict"]
    assert v["verdict"] == "reject"
    assert "scope" in v["rationale"]
    assert any("empty globs" in i.get("message", "") for i in v["issues"])


def test_merge_verdicts_fenced_json() -> None:
    state = {
        "verify_correctness": '```json\n{"verdict": "reject", "rationale": "x"}\n```',
        "verify_scope": "pass",
        "verify_risk": "pass",
    }
    _run(ps.merge_verdicts(_FakeCtx(state)))
    assert state["verifier_verdict"]["verdict"] == "reject"


def test_merge_verdicts_unparseable_defaults_pass() -> None:
    state = {"verify_correctness": "garbage", "verify_scope": "", "verify_risk": None}
    _run(ps.merge_verdicts(_FakeCtx(state)))
    assert state["verifier_verdict"]["verdict"] == "pass"


def test_merge_verdicts_matches_runner_extractor() -> None:
    from aiforge_core.runtime.adk_runner import _extract_verifier
    state = {
        "verify_correctness": '{"verdict": "reject", "rationale": "r"}',
        "verify_scope": '{"verdict": "pass"}',
        "verify_risk": '{"verdict": "pass"}',
    }
    _run(ps.merge_verdicts(_FakeCtx(state)))
    assert _extract_verifier(state) == "reject"


# ── builders ─────────────────────────────────────────────────────────────

@pytest.fixture
def _no_escalate(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")


def test_build_context_branches(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    branches = ps.build_context_branches(build_litellm_model)
    assert [b.name for b in branches] == [
        "researcher", "ctx_repomap", "ctx_conventions"]


def test_build_context_branches_skip_researcher(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    branches = ps.build_context_branches(build_litellm_model, skip_researcher=True)
    names = [b.name for b in branches]
    assert "researcher" not in names
    assert names == ["ctx_repomap", "ctx_conventions"]


def test_build_verifier_branches(_no_escalate) -> None:
    from aiforge_core.runtime.pipeline import build_litellm_model
    branches = ps.build_verifier_branches(build_litellm_model)
    assert [b.name for b in branches] == [
        "verify_correctness", "verify_scope", "verify_risk"]


def test_join_and_merge_node_names() -> None:
    assert ps.make_context_join().name == "context_join"
    assert ps.make_verifier_join().name == "verifier_join"
    assert ps.make_merge_context_node().name == "merge_context"
    assert ps.make_merge_verdicts_node().name == "merge_verdicts"
