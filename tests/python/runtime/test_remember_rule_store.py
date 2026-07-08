"""A rule built in chat (remember_rule) must land in the SAME store the Library
UI reads (repo_rules → ~/.aiforge/rules/), be injected into the agent every
turn, AND be recorded in memory (global vs repo-scoped). Previously it wrote to
md_store only, so it never showed in the Library.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def test_remember_rule_lands_in_library_store(cfg):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime import repo_rules
    r = ca._t_remember_rule({"text": "Always run tests before commit",
                             "description": "run the test suite before every commit",
                             "triggers": ["commit", "test"],
                             "scope": "global"}, ".")
    assert r["ok"]
    rules = repo_rules.load_global_rules()                        # Library reads this
    # Unified frontmatter: name is the authoritative `name:` field, plus the
    # new description / triggers / scope round-trip.
    names = [x.name for x in rules]
    assert "Always run tests before commit" in names
    rule = next(x for x in rules if x.name == "Always run tests before commit")
    assert rule.description == "run the test suite before every commit"
    assert "commit" in rule.triggers and "test" in rule.triggers
    assert rule.scope == "global"


def test_remember_rule_reaches_agent_context(cfg):
    from aiforge_core.runtime import chat_agent as ca
    ca._t_remember_rule({"text": "Never commit secrets", "scope": "global"}, ".")
    ctx = ca._rules_context(".")
    assert "Never commit secrets" in ctx


def test_remember_rule_writes_memory_scoped(cfg):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.memory import sqlite_memory as m
    ca._t_remember_rule({"text": "Global rule X", "scope": "global"}, ".")
    ca._t_remember_rule({"text": "Repo rule Y", "scope": "repo"}, ".")
    with m._conn() as c:
        rows = {r["text"]: r["repo"]
                for r in c.execute("SELECT text, repo FROM memory_units").fetchall()}
    assert rows.get("RULE: Global rule X") is None        # global = repo-agnostic
    assert rows.get("RULE: Repo rule Y") is not None       # repo-scoped


def test_builder_elaborates_body_via_llm(cfg, monkeypatch):
    from unittest.mock import patch
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime import skills

    def fake(role, messages, **kw):
        return "# Title\n\n1. Elaborated step one.\n2. Step two.\n3. Verify."
    with patch("aiforge_core.llm.client.complete", fake):
        r = ca._t_learn_skill({"name": "el-skill", "body": "do the thing",
                               "scope": "global"}, ".")
    assert r["ok"]
    body = [s for s in skills.load() if s.name == "el-skill"][0].body
    assert "Elaborated step one" in body and "do the thing" not in body


def test_builder_elaborate_fallback_keeps_raw(cfg, monkeypatch):
    from unittest.mock import patch
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime import skills

    def boom(role, messages, **kw):
        raise RuntimeError("llm down")
    with patch("aiforge_core.llm.client.complete", boom):
        r = ca._t_learn_skill({"name": "raw-skill", "body": "raw text",
                               "scope": "global"}, ".")
    assert r["ok"]
    body = [s for s in skills.load() if s.name == "raw-skill"][0].body
    assert "raw text" in body            # never lost when elaboration fails


def test_repo_rule_overrides_global_in_chat(cfg, tmp_path):
    import os
    from aiforge_core.runtime import repo_rules, chat_agent as ca
    repo_rules.write_rule("style", "GLOBAL: use 4 spaces", always=True)
    rdir = tmp_path / ".aiforge" / "rules"
    rdir.mkdir(parents=True)
    (rdir / "style.md").write_text(
        "---\nname: style\nalwaysApply: true\n---\nREPO: use tabs\n")
    ctx = ca._rules_context(str(tmp_path))
    assert "REPO: use tabs" in ctx            # repo rule wins
    assert "GLOBAL: use 4 spaces" not in ctx  # same-name global suppressed


def test_repo_rule_overrides_global_in_pipeline(cfg, tmp_path):
    # The team/pipeline path uses load_rules — same name-precedence.
    from aiforge_core.runtime import repo_rules
    repo_rules.write_rule("style", "GLOBAL body", always=True)
    rdir = tmp_path / ".aiforge" / "rules"
    rdir.mkdir(parents=True)
    (rdir / "style.md").write_text(
        "---\nname: style\nalwaysApply: true\n---\nREPO body\n")
    bodies = {r.name: r.body for r in repo_rules.load_rules(str(tmp_path))}
    assert "REPO body" in bodies["style"] and "GLOBAL body" not in bodies["style"]


def test_repo_skill_and_workflow_override_global(cfg, tmp_path):
    import os
    from aiforge_core.runtime import skills, workflows
    skills.write_skill("deploy", "d", "GLOBAL deploy steps", ["deploy"],
                       scope="global")
    workflows.write_workflow("ship", "d", "GLOBAL ship steps", ["ship"],
                             scope="global")
    os.makedirs(tmp_path / ".aiforge" / "skills" / "deploy")
    (tmp_path / ".aiforge" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: d\ntriggers: [deploy]\n---\nREPO deploy steps\n")
    os.makedirs(tmp_path / ".aiforge" / "workflows" / "ship")
    (tmp_path / ".aiforge" / "workflows" / "ship" / "WORKFLOW.md").write_text(
        "---\nname: ship\ndescription: d\ntriggers: [ship]\n---\nREPO ship steps\n")
    # load precedence (what every mode's auto_context uses)
    sk = {s.name: s.body for s in skills.load(str(tmp_path))}
    wf = {w.name: w.body for w in workflows.load(str(tmp_path))}
    assert "REPO deploy steps" in sk["deploy"] and "GLOBAL" not in sk["deploy"]
    assert "REPO ship steps" in wf["ship"] and "GLOBAL" not in wf["ship"]
    # the injected block reflects the override
    assert "REPO deploy steps" in skills.auto_context("please deploy", str(tmp_path))
    assert "REPO ship steps" in workflows.auto_context("please ship", str(tmp_path))
