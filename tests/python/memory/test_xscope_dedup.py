"""Cross-scope dedup + topic snap (production-grade scale fixes)."""
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


def _brief(md_store, key, facts):
    from aiforge_core.runtime import work_notes
    (md_store.memory_dir() / f"compacted-{key}.md").write_text(
        work_notes.render_note("knowledge", key, title=key, objective="d.",
                               facts=facts, updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")


def test_dedupe_global_copies(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime.work_notes import parse_note
    _brief(md_store, "shared", ["never commit directly to main", "run tests first"])
    _brief(md_store, "svc", ["never commit directly to main", "OrderController maps /orders"])
    _brief(md_store, "data-sync", ["run tests first", "last-write-wins on updatedAt"])
    res = md_store.dedupe_global_copies()
    assert res["removed"] == 2
    svc = parse_note((md_store.memory_dir() / "compacted-svc.md").read_text())["sections"]["facts"]
    assert not any("commit directly" in f for f in svc)      # global copy dropped
    assert any("OrderController" in f for f in svc)           # own fact kept
    shared = parse_note((md_store.memory_dir() / "compacted-shared.md").read_text())["sections"]["facts"]
    assert any("commit directly" in f for f in shared)        # global kept


def test_snap_topic_merges_near_duplicate(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    _brief(md_store, "sync-retries", ["x"])   # existing topic brief

    def _fake(role, messages, model, *a, **k):
        return types.SimpleNamespace(scope="topic", repo="", topic="sync-retrys")  # typo/variant

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    d = md_store.classify_scope("some sync fact")
    assert d["topic"] == "sync-retries"        # snapped to existing, not a new brief


def test_reconcile_briefs_collapses_cross_scope(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    monkeypatch.setenv("AIFORGE_OKR_RECONCILE", "1")
    import types
    from aiforge_core.memory import md_store
    from aiforge_core.runtime.work_notes import parse_note
    # deploy contradiction scattered across two project briefs
    _brief(md_store, "repo-a", ["deploy uses docker compose up"])
    _brief(md_store, "repo-b", ["deploy uses systemctl restart, not docker",
                                "OrderController maps /orders"])

    def _fake(role, messages, model, *a, **k):
        # drop the stale docker fact from repo-a
        return types.SimpleNamespace(removes=[
            types.SimpleNamespace(scope="repo-a", fact="deploy uses docker compose up")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.reconcile_briefs()
    assert res["removed"] == 1
    a = parse_note((md_store.memory_dir() / "compacted-repo-a.md").read_text())["sections"].get("facts", [])
    assert not any("docker" in f for f in a)               # stale dropped
    b = parse_note((md_store.memory_dir() / "compacted-repo-b.md").read_text())["sections"]["facts"]
    assert any("systemctl" in f for f in b)                # canonical kept
    assert any("OrderController" in f for f in b)          # unrelated untouched


def test_reconcile_disabled(mem):
    from aiforge_core.memory import md_store
    assert md_store.reconcile_briefs()["removed"] == 0     # suite LLM off
