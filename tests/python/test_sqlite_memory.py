import importlib

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def test_write_and_recall(mem):
    mem.write_unit(text="configure the neo4j connection pool",
                   kind="learning", source="learner", repo="demo")
    mem.write_unit(text="bake a chocolate cake with butter",
                   kind="learning", source="learner", repo="demo")
    hits = mem.recall("how do I configure neo4j", limit=5, repo="demo")
    assert hits
    assert "neo4j" in hits[0]["text"]
    assert 0.0 <= hits[0]["score"] <= 1.0


def test_empty_text_skipped(mem):
    assert mem.write_unit(text="   ", kind="x") == 0
    assert mem.recall("anything") == []


def test_exact_dedupe_same_repo(mem):
    a = mem.write_unit(text="same text", repo="demo")
    b = mem.write_unit(text="same text", repo="demo")
    assert a > 0
    assert b == 0


def test_dedupe_is_repo_scoped(mem):
    a = mem.write_unit(text="same text", repo="r1")
    b = mem.write_unit(text="same text", repo="r2")
    assert a > 0 and b > 0


def test_repo_filter_includes_repo_agnostic(mem):
    mem.write_unit(text="alpha configuration detail", repo="r1")
    mem.write_unit(text="alpha configuration detail global", repo=None)
    mem.write_unit(text="alpha configuration detail other", repo="r2")
    hits = mem.recall("alpha configuration", limit=10, repo="r1")
    repos = {h["repo"] for h in hits}
    assert "r2" not in repos
    assert "r1" in repos or None in repos


def test_stats(mem):
    mem.write_unit(text="one", kind="learning")
    mem.write_unit(text="two", kind="failure")
    s = mem.stats()
    assert s["backend"] == "sqlite"
    assert s["total"] == 2
    assert s["by_kind"].get("failure") == 1
