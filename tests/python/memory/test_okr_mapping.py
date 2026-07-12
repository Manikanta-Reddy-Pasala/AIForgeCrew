"""T3 — cross-scope mapping between OKR memory briefs.

Projects map to global memory and global maps back — bidirectionally. A brief
lives flat as ``compacted-<scope>.md``; a mapping is a same-directory relative
link to the sibling brief, added to BOTH briefs' Links section. map_scopes()
asks the learner LLM which briefs relate, then writes the links.
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
        objective="Durable knowledge for this scope.", facts=facts,
        updated_at="2026-07-12T00:00:00+00:00")
    p = md_store.memory_dir() / f"compacted-{key}.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_normalize_links_keeps_sibling_brief_link():
    from aiforge_core.runtime.work_notes import normalize_links
    out = normalize_links(["[global](compacted-shared.md)"],
                          "knowledge", "oneshell-pos")
    assert out == ["[global](compacted-shared.md)"]


def test_normalize_links_dedupes_sibling_brief_link():
    from aiforge_core.runtime.work_notes import normalize_links
    out = normalize_links(["[global](compacted-shared.md)",
                           "[global](compacted-shared.md)"],
                          "knowledge", "oneshell-pos")
    assert out == ["[global](compacted-shared.md)"]


def test_map_scopes_links_related_briefs_bidirectionally(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _write_brief(md_store, "shared", ["use && not ; to gate deploy commands"])
    _write_brief(md_store, "oneshell-pos", ["deploy via git pull + restart"])

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(
            edges=[{"a": "shared", "b": "oneshell-pos", "reason": "deploy"}])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.map_scopes()
    assert res["edges"] >= 1

    from aiforge_core.runtime.work_notes import parse_note
    shared = parse_note((md_store.memory_dir() / "compacted-shared.md").read_text())
    pos = parse_note((md_store.memory_dir() / "compacted-oneshell-pos.md").read_text())
    assert any("compacted-oneshell-pos.md" in l
               for l in shared["sections"].get("links", []))
    assert any("compacted-shared.md" in l
               for l in pos["sections"].get("links", []))


def test_map_scopes_accepts_from_to_edge_keys(monkeypatch, mem):
    """The model often returns {from,to} rather than {a,b} — accept both."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _write_brief(md_store, "shared", ["a global fact"])
    _write_brief(md_store, "svc", ["a repo fact"])

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(edges=[{"from": "shared", "to": "svc"}])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.map_scopes()
    assert res["edges"] == 1


def test_map_scopes_noop_when_llm_off(mem):
    from aiforge_core.memory import md_store
    _write_brief(md_store, "shared", ["x"])
    _write_brief(md_store, "svc", ["y"])
    res = md_store.map_scopes()  # suite default AIFORGE_OKR_SCOPE_LLM=0
    assert res["edges"] == 0


def test_map_scopes_ignores_unknown_brief_keys(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _write_brief(md_store, "shared", ["x"])
    _write_brief(md_store, "svc", ["y"])

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(
            edges=[{"a": "shared", "b": "does-not-exist"},
                   {"a": "shared", "b": "shared"}])  # self-edge ignored too

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.map_scopes()
    assert res["edges"] == 0
