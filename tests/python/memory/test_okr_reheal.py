"""T8 — self-heal mis-scoped facts.

A global fact that ended up in a project brief (captured before scope
classification, or mis-hinted) is re-classified and MOVED to the shared brief on
a reheal pass. Gated on AIFORGE_OKR_SCOPE_LLM. Deterministic-off → no-op.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def _write_brief(md_store, key, facts):
    from aiforge_core.runtime import work_notes
    text = work_notes.render_note(
        "knowledge", key, title=f"{key} brief",
        objective="Durable knowledge.", facts=facts,
        updated_at="2026-07-12T00:00:00+00:00")
    (md_store.memory_dir() / f"compacted-{key}.md").write_text(text, encoding="utf-8")


def test_reheal_moves_global_fact_to_shared(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _write_brief(md_store, "svc",
                 ["always run tests before commit", "OrderController maps /orders"])

    def _fake(role, messages, model, *a, **k):
        c = messages[-1]["content"]
        scope = "global" if "tests" in c else "project"
        return types.SimpleNamespace(scope=scope, repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.reheal_scopes()
    assert res["moved"] == 1

    from aiforge_core.runtime.work_notes import parse_note
    svc = parse_note((md_store.memory_dir() / "compacted-svc.md").read_text())
    facts = svc["sections"].get("facts", [])
    assert not any("tests" in f for f in facts)
    assert any("OrderController" in f for f in facts)
    assert (md_store.memory_dir() / "compacted-shared.md").exists()


def test_reheal_noop_when_llm_off(mem):
    from aiforge_core.memory import md_store
    _write_brief(md_store, "svc", ["always run tests before commit"])
    assert md_store.reheal_scopes()["moved"] == 0


def test_reheal_leaves_shared_brief_alone(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _write_brief(md_store, "shared", ["a global fact"])

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="project", repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    # shared is never demoted into a project → nothing moves
    assert md_store.reheal_scopes()["moved"] == 0
