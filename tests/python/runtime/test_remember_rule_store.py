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
                             "scope": "global"}, ".")
    assert r["ok"]
    names = [x.name for x in repo_rules.load_global_rules()]      # Library reads this
    assert "always-run-tests-before-commit" in names


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
