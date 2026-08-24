"""Workflow-transparency: skills/rules/workflows usage surfaced on the graph."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    sk = tmp_path / "skills"
    sk.mkdir()
    (sk / "always-one").mkdir()
    (sk / "always-one" / "SKILL.md").write_text(
        "---\nname: always-one\ndescription: d\nalways: true\n---\nbody\n")
    (sk / "match-py").mkdir()
    (sk / "match-py" / "SKILL.md").write_text(
        "---\nname: match-py\ndescription: python helper\ntriggers: [python]\n---\nb\n")
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(sk))
    monkeypatch.setenv("AIFORGE_SKILLS_ALWAYS_CAP", "8")
    return sk


def test_skills_selected_names_marks_why(skills_env):
    from aiforge_core.runtime import skills
    names = skills.selected_names("write python code", None, k=5)
    by = {n["name"]: n["why"] for n in names}
    assert by.get("always-one") == "always"
    assert by.get("match-py") == "match"


def test_skills_auto_context_unchanged_behaviour(skills_env):
    from aiforge_core.runtime import skills
    block = skills.auto_context("write python code", None)
    assert "always-one" in block
    assert "match-py" in block


def test_workflows_selected_names(tmp_path, monkeypatch):
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "release").mkdir()
    (wf / "release" / "WORKFLOW.md").write_text(
        "---\nname: release\ndescription: cut a release\ntriggers: [release]\n---\nsteps\n")
    monkeypatch.setenv("AIFORGE_WORKFLOWS_DIR", str(wf))
    from aiforge_core.runtime import workflows
    names = workflows.selected_names("how to cut a release", None, k=3)
    assert {"name": "release", "why": "match"} in names


def test_repo_rules_matched_names(tmp_path):
    from aiforge_core.runtime import repo_rules
    rdir = tmp_path / ".aiforge" / "rules"
    rdir.mkdir(parents=True)
    (rdir / "style.md").write_text(
        "---\nname: style\nalways: true\n---\nuse 4 spaces\n")
    got = repo_rules.matched_names(str(tmp_path), None)
    assert any(r["name"] == "style" for r in got)


def test_emit_context_injected_noop_when_empty(monkeypatch):
    from aiforge_core.runtime import observability as obs
    calls = []
    monkeypatch.setattr(obs, "_emit", lambda **kw: calls.append(kw))
    obs.emit_context_injected(ticket_id=1)
    assert calls == []


def test_emit_context_injected_payload(monkeypatch):
    from aiforge_core.runtime import observability as obs
    calls = []
    monkeypatch.setattr(obs, "_emit", lambda **kw: calls.append(kw))
    obs.emit_context_injected(
        ticket_id=7, skills=[{"name": "s", "why": "match"}],
        rules=[{"name": "r", "source": "x"}])
    assert len(calls) == 1
    md = calls[0]["metadata"]
    assert md["skills"] == [{"name": "s", "why": "match"}]
    assert md["rules"] == [{"name": "r", "source": "x"}]
    assert calls[0]["kind"] == "context_injected"


def test_topology_overlay_attaches_context(monkeypatch):
    from aiforge_core.runtime import workflow_topology as wt

    class _T:
        id = 99

    events = [
        {"agent_role": "doer", "kind": "stage_start", "metadata": {},
         "created_at": "2026-06-30T10:00:00Z"},
        {"agent_role": "pipeline", "kind": "context_injected",
         "created_at": "2026-06-30T10:00:01Z",
         "metadata": {"skills": [{"name": "s1", "why": "always"}],
                      "workflows": [{"name": "w1", "why": "match"}],
                      "rules": [{"name": "r1", "source": "AGENTS.md"}]}},
        # duplicate skill from a replan — must de-dupe
        {"agent_role": "pipeline", "kind": "context_injected",
         "created_at": "2026-06-30T10:05:00Z",
         "metadata": {"skills": [{"name": "s1", "why": "always"}]}},
    ]

    # Patch the real store module's functions (workflow_topology does
    # `from aiforge_core.tickets import store as _store` per call, so patching
    # sys.modules is order-fragile — patch the bound attributes instead).
    from aiforge_core.tickets import store as real_store
    monkeypatch.setattr(real_store, "get", lambda _ident: _T())
    monkeypatch.setattr(real_store, "comments", lambda _tid, _limit: events)
    snap = wt.snapshot("ONE-1")
    ctx = snap["context"]
    assert [s["name"] for s in ctx["skills"]] == ["s1"]  # de-duped
    assert [w["name"] for w in ctx["workflows"]] == ["w1"]
    assert [r["name"] for r in ctx["rules"]] == ["r1"]
    doer = next(n for n in snap["nodes"] if n["id"] == "doer")
    assert doer["skills"] == [{"name": "s1", "why": "always"}]
    # non-consumer node (a gate) stays empty
    gate = next(n for n in snap["nodes"] if n["id"] == "loop_gate")
    assert gate["skills"] == []
