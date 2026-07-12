"""T1 — LLM scope classifier for OKR memory.

A captured fact belongs to exactly one scope:
  - global          → cross-project knowledge (tooling, conventions, stack)   → compacted-shared.md
  - project:<repo>  → only meaningful for one repo                            → compacted-<repo>.md
  - topic:<slug>    → a cross-cutting theme/workflow                          → compacted-<slug>.md

classify_scope decides this from the text (+ optional hints). It uses the
configured learner role LLM when AIFORGE_OKR_SCOPE_LLM != "0"; when the LLM is
off or unreachable it falls back DETERMINISTICALLY to the caller's hints (so
existing behaviour and tests are unchanged). The LLM may PROMOTE a
repo-hinted fact to global when the content is clearly cross-project.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    return None


def test_fallback_honors_repo_hint(no_llm):
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("OrderService retries 3x on NATS timeout",
                       hint_repo="oneshell-pos")
    assert d["scope"] == "project"
    assert d["repo"] == "oneshell-pos"
    assert d["topic"] is None


def test_fallback_topic_when_only_topic_hint(no_llm):
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("prefer last-write-wins on updatedAt",
                       hint_topic="Data Sync")
    assert d["scope"] == "topic"
    assert d["topic"] == "data-sync"
    assert d["repo"] is None


def test_fallback_global_when_no_hints(no_llm):
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("use && not ; to gate deploy commands")
    assert d["scope"] == "global"
    assert d["repo"] is None
    assert d["topic"] is None


def test_llm_promotes_repo_hint_to_global(monkeypatch):
    """A globally-true fact captured under a repo is re-routed to global."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="global", repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("always run tests before commit",
                       hint_repo="oneshell-pos")
    assert d["scope"] == "global"
    assert d["repo"] is None


def test_llm_failure_degrades_to_hint(monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("some repo-specific detail", hint_repo="svc")
    assert d["scope"] == "project"
    assert d["repo"] == "svc"


def test_llm_assigns_project_from_hint_repo(monkeypatch):
    """LLM says project but returns no repo → use the hint repo."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="project", repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    from aiforge_core.memory.md_store import classify_scope
    d = classify_scope("OrderController maps /orders", hint_repo="svc")
    assert d["scope"] == "project"
    assert d["repo"] == "svc"


# ── capture() wiring: promote a repo-hinted global fact to the shared brief ───

@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def test_capture_promotes_repo_fact_to_shared(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="global", repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    from aiforge_core.memory import md_store
    res = md_store.capture("project_learning", "always run tests before commit",
                           repo="oneshell-pos")
    assert res["repo"] == "shared"
    assert not any(t.startswith("repo:") for t in res.get("tags", []))
    # brief maintenance landed in the shared brief, not the repo brief
    assert (md_store.brief_path("shared")).exists()
    assert not (md_store.brief_path("oneshell-pos")).exists()


def test_capture_keeps_repo_fact_when_not_promoted(monkeypatch, mem):
    """LLM confirms project scope → fact stays with its repo (no regression)."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="project", repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    from aiforge_core.memory import md_store
    res = md_store.capture("project_learning", "OrderController maps /orders",
                           repo="oneshell-pos")
    assert res["repo"] == "oneshell-pos"
    assert "repo:oneshell-pos" in res.get("tags", [])


def test_capture_no_classify_when_llm_off(mem):
    """Deterministic path (suite default LLM off): repo hint is untouched."""
    from aiforge_core.memory import md_store
    res = md_store.capture("project_learning", "always run tests before commit",
                           repo="oneshell-pos")
    assert res["repo"] == "oneshell-pos"
