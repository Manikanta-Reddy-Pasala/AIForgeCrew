"""Tests for glob-scoped repo rules (runtime.repo_rules) + wiring."""
from __future__ import annotations

import asyncio

import pytest  # noqa: E402

from aiforge_core.runtime import repo_rules


@pytest.fixture(autouse=True)
def _unified_query_isolation():
    """Tests here import adk_runner/pipeline, which transitively cache
    unified_query on its parent package — defeating test_doer_tools'
    sys.modules-patch isolation (the landmine test_unified_query_afm
    documents). Clean after every test in this file."""
    yield
    import sys

    import aiforge_core.memory as _mem
    sys.modules.pop("aiforge_core.memory.unified_query", None)
    if hasattr(_mem, "unified_query"):
        delattr(_mem, "unified_query")


def _seed_repo(tmp_path):
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "python.mdc").write_text(
        "---\ndescription: python style\nglobs: src/**\n---\n"
        "Use ruff. Max line 100."
    )
    (tmp_path / ".cursor" / "rules" / "docs.mdc").write_text(
        "---\ndescription: docs style\nglobs: docs/**\n---\n"
        "Write docs in present tense."
    )
    (tmp_path / ".aiforge" / "rules").mkdir(parents=True)
    (tmp_path / ".aiforge" / "rules" / "always.md").write_text(
        "---\ndescription: golden rule\nalwaysApply: true\n---\n"
        "Never commit secrets."
    )
    (tmp_path / "AGENTS.md").write_text("Run make test before any commit.")


def test_load_rules_all_sources(tmp_path):
    _seed_repo(tmp_path)
    rules = repo_rules.load_rules(tmp_path)
    names = {r.name for r in rules}
    assert {"python style", "docs style", "golden rule", "AGENTS.md"} <= names


def test_match_scopes_by_glob(tmp_path):
    _seed_repo(tmp_path)
    rules = repo_rules.load_rules(tmp_path)
    matched = repo_rules.match_rules(rules, ["src/app/**"])
    names = {r.name for r in matched}
    assert "python style" in names      # src/** ∩ src/app/**
    assert "docs style" not in names    # docs/** doesn't touch src
    assert "golden rule" in names       # alwaysApply
    assert "AGENTS.md" in names         # always-on file


def test_match_no_scope_includes_always_only(tmp_path):
    _seed_repo(tmp_path)
    rules = repo_rules.load_rules(tmp_path)
    matched = repo_rules.match_rules(rules, [])
    names = {r.name for r in matched}
    assert names == {"golden rule", "AGENTS.md"}


def test_collect_renders_and_caps(tmp_path, monkeypatch):
    _seed_repo(tmp_path)
    out = repo_rules.collect(tmp_path, ["src/**"])
    assert "Use ruff" in out
    assert "Never commit secrets" in out
    assert "present tense" not in out
    # empty repo → empty string, never raises
    assert repo_rules.collect(tmp_path / "nope", ["x"]) == ""


def test_skip_conventions_drops_branch(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    from aiforge_core.runtime.parallel_stages import build_context_branches
    from aiforge_core.runtime.pipeline import build_litellm_model
    names = [b.name for b in build_context_branches(
        build_litellm_model, skip_researcher=True, skip_conventions=True)]
    assert names == ["ctx_repomap"]


def test_plan_promote_refreshes_rules(tmp_path, monkeypatch):
    """Once the plan widens scope, file-scoped rules load via the
    promote node."""
    _seed_repo(tmp_path)
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import graph_pipeline as gp

    class _Ctx:
        def __init__(self, state):
            self.state = state
            self.route = None

    plan = '{"subtickets": [{"scope_allowlist_globs": ["src/x/**"]}]}'
    state = {"plan_md": plan}
    asyncio.run(gp._plan_promote(_Ctx(state)))
    assert "Use ruff" in state.get("rules_md", "")


def test_memory_brief_seeded_not_in_prompt(monkeypatch):
    """Audit fix: memory block rides state, not the replayed seed."""
    from aiforge_core.runtime.adk_runner import _build_prompt

    class _T:
        identifier = "ONE-1"
        title = "t"
        body = "b"
        metadata = None
        id = 1
    out = _build_prompt(_T(), "## Memory hits\n- fact")
    assert "Memory hits" not in out  # no longer stitched into the seed


def test_context_branches_exclude_ctx_memory(monkeypatch):
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    from aiforge_core.runtime.parallel_stages import build_context_branches
    from aiforge_core.runtime.pipeline import build_litellm_model
    names = [b.name for b in build_context_branches(
        build_litellm_model, skip_researcher=False)]
    assert "ctx_memory" not in names
    assert names[0] == "researcher"


def test_diversify_groups_afm_sections():
    from aiforge_core.memory.unified_query import _diversify
    hits = (
        [{"source": "afm_bundle", "group": "afm:chunk", "text": f"c{i}"}
         for i in range(4)]
        + [{"source": "afm_bundle", "group": "afm:observation",
            "text": f"o{i}"} for i in range(2)]
    )
    out = _diversify(hits, per_group=3)
    # chunks capped at 3, observations keep their own budget
    assert sum(1 for h in out if h["group"] == "afm:chunk") == 3
    assert sum(1 for h in out if h["group"] == "afm:observation") == 2


def test_trajectory_touched_paths():
    from aiforge_core.runtime.trajectory import _touched_paths
    events = [
        {"args": "{'path': 'src/app/main.py'}"},
        {"text": "edited aiforge_core/runtime/pipeline.py and ran tests"},
        {"args": "no paths here"},
        {"args": "{'path': 'src/app/main.py'}"},  # dupe
    ]
    paths = _touched_paths(events)
    assert "src/app/main.py" in paths
    assert "aiforge_core/runtime/pipeline.py" in paths
    assert len([p for p in paths if p == "src/app/main.py"]) == 1


def test_globs_intersect_extension_vs_dir():
    """v1 matcher failed the dominant real combo — Cursor extension
    globs vs ticket directory scopes."""
    from aiforge_core.runtime.repo_rules import _globs_intersect as gi
    assert gi("**/*.py", "src/a/**")
    assert gi("*.py", "aiforge_core/runtime/**")
    assert gi("aiforge_core/**/*.py", "aiforge_core/runtime/**")
    assert gi("app/**/*.ts", "app/routes/**")
    assert not gi("docs/**", "src/**")
    assert not gi("backend/**/*.go", "frontend/**")
