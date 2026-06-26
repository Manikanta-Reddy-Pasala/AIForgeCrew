"""Tests for the context-aware Enhancer + Architect in parallel_subtasks.

The Enhancer is mandatory in every chat mode: it fixes spelling/grammar,
recalls context (memory + recent conversation + repo README), and folds it
into a build spec. The Architect designs the file structure honoring the
repo's skills/workflows/rules. Both are hermetic here — the LLM client and
the memory backend are monkeypatched, so there is no network/Neo4j.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import parallel_subtasks as pp


# ─── Enhancer ─────────────────────────────────────────────────────────


def test_enhance_incorporates_memory_hit(monkeypatch):
    """The enhanced spec is built from a user message that carries the
    recalled memory hit, and the enhancer's output is returned."""
    seen: dict = {}

    def fake_query(text, **kw):
        return {"hits": [{"text": "PRIOR_FACT: use SQLite for the store"}],
                "used_sources": ["memory"], "errors": []}

    def fake_complete(role, convo, **kw):
        seen["role"] = role
        seen["user"] = convo[-1]["content"]
        return "GOAL: build a store\n- uses SQLite"

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", fake_query)
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    out = pp._enhance("buld a stor for products and customers",
                      cwd=None, repo="demo")
    assert out == "GOAL: build a store\n- uses SQLite"
    assert seen["role"] == "enhancer"
    # The recalled memory fact must be folded into the prompt sent to the LLM.
    assert "PRIOR_FACT: use SQLite for the store" in seen["user"]
    assert "RELEVANT MEMORY" in seen["user"]
    assert "buld a stor" in seen["user"]


def test_enhance_includes_recent_conversation(monkeypatch):
    seen: dict = {}

    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "spec"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    current = "add a list command to the CLI tool"
    history = [
        {"role": "user", "content": "we are building a CLI"},
        {"role": "assistant", "content": "ok, what should it do?"},
        {"role": "user", "content": current},   # current msg
    ]
    out = pp._enhance(current, history=history)
    assert out == "spec"
    assert "RECENT CONVERSATION" in seen["user"]
    assert "we are building a CLI" in seen["user"]
    # current (last) user message excluded from the conversation block, but it
    # is still the USER REQUEST.
    assert seen["user"].count(current) == 1


def test_enhance_falls_back_to_raw_on_client_error(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    assert pp._enhance("raw prompt here that needs real enhancement") \
        == "raw prompt here that needs real enhancement"


def test_enhance_falls_back_on_empty_output(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "   ")
    assert pp._enhance("raw prompt that should enhance into something") \
        == "raw prompt that should enhance into something"


def test_enhance_respects_disable_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_ENHANCER_DISABLE", "1")

    def should_not_be_called(*a, **k):
        raise AssertionError("LLM must not be called when enhancer disabled")

    monkeypatch.setattr("aiforge_core.llm.client.complete", should_not_be_called)
    assert pp._enhance("untouched prompt") == "untouched prompt"


def test_enhance_backward_compatible_single_arg(monkeypatch):
    """Existing callers pass just the prompt — must still work."""
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced")
    assert pp._enhance("a prompt to build a parser module") == "enhanced"


def test_enhance_reads_repo_readme(monkeypatch, tmp_path):
    seen: dict = {}
    (tmp_path / "README.md").write_text("# Demo Project\nThis builds widgets.\n")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "spec"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    pp._enhance("do a thing that builds widgets nicely", cwd=str(tmp_path))
    assert "REPO README" in seen["user"]
    assert "This builds widgets." in seen["user"]


# ─── Enhancer triviality gate (M7) ────────────────────────────────────


def test_enhance_skips_trivial_short_prompt(monkeypatch):
    # Short prompt → enhancer is skipped entirely (no memory, no LLM).
    def _must_not_call(*a, **k):
        raise AssertionError("LLM/memory must not run for a trivial prompt")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _must_not_call)
    monkeypatch.setattr("aiforge_core.llm.client.complete", _must_not_call)
    assert pp._enhance("hi") == "hi"
    assert pp._enhance("fix the bug") == "fix the bug"   # < 24 chars


def test_enhance_skips_conversational_prompt(monkeypatch):
    def _must_not_call(*a, **k):
        raise AssertionError("LLM/memory must not run for chit-chat")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _must_not_call)
    monkeypatch.setattr("aiforge_core.llm.client.complete", _must_not_call)
    for p in ("thanks, that works great!", "good morning, how are you",
              "ok cool", "who are you"):
        assert pp._enhance(p) == p


def test_enhance_min_chars_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_ENHANCER_MIN_CHARS", "5")
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced")
    # 10 chars, non-conversational → above the lowered threshold → enhanced.
    assert pp._enhance("parse json") == "enhanced"


# ─── Architect ────────────────────────────────────────────────────────


def test_architect_injects_skills_workflows_rules(monkeypatch):
    seen: dict = {}

    monkeypatch.setattr(
        "aiforge_core.runtime.skills.auto_context",
        lambda q, cwd=None, **k: "SKILL_BLOCK: use the doc skill")
    monkeypatch.setattr(
        "aiforge_core.runtime.workflows.auto_context",
        lambda q, cwd=None, **k: "WORKFLOW_BLOCK: ship-it procedure")
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect",
        lambda cwd, *a, **k: "RULE_BLOCK: always add tests")

    def fake_complete(role, convo, **kw):
        seen["role"] = role
        seen["user"] = convo[-1]["content"]
        return json.dumps({"files": [
            {"path": "db.py", "purpose": "store"},
            {"path": "main.py", "purpose": "entry"}]})

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    files = pp._architect("build a store", cwd="/some/repo")
    # Output contract preserved.
    assert files == [
        {"path": "db.py", "purpose": "store"},
        {"path": "main.py", "purpose": "entry"}]
    assert seen["role"] == "architect"
    # All three injected context blocks reach the architect's user message.
    assert "SKILLS:" in seen["user"]
    assert "SKILL_BLOCK: use the doc skill" in seen["user"]
    assert "WORKFLOWS:" in seen["user"]
    assert "WORKFLOW_BLOCK: ship-it procedure" in seen["user"]
    assert "REPO RULES:" in seen["user"]
    assert "RULE_BLOCK: always add tests" in seen["user"]


def test_architect_backward_compatible_no_cwd(monkeypatch):
    """Existing callers pass just the spec; with no cwd, rules are skipped
    but skills/workflows still attempt (and the JSON still parses)."""
    monkeypatch.setattr("aiforge_core.runtime.skills.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.workflows.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.repo_rules.collect",
                        lambda *a, **k: "")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: json.dumps({"files": [{"path": "x.py", "purpose": "p"}]}))
    assert pp._architect("a spec") == [{"path": "x.py", "purpose": "p"}]


def test_architect_context_soft_fails(monkeypatch):
    """A crash in any context source must not break the architect."""
    def boom(*a, **k):
        raise RuntimeError("skills blew up")

    monkeypatch.setattr("aiforge_core.runtime.skills.auto_context", boom)
    monkeypatch.setattr("aiforge_core.runtime.workflows.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.repo_rules.collect",
                        lambda *a, **k: "")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: json.dumps({"files": [{"path": "y.py", "purpose": "q"}]}))
    assert pp._architect("spec", cwd="/repo") == [{"path": "y.py", "purpose": "q"}]
