"""OKF 'one concept = one file': a repeated write of the same concept REUSES its
file instead of minting a new incrementing id (the L-01/L-07/L-13 leak), and
dedupe_nodes collapses near-duplicate paraphrases within a scope."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.okf.store as s
    return importlib.reload(s)


def _learnings(store, scope_meta=None):
    return [d for d in store.load_all() if d.get("type") == "learning"
            and (scope_meta is None or True)]


def test_exact_same_concept_reuses_file(store):
    meta = {"scope": "global"}
    r1 = store.save_node("learning", None, meta, "Always paginate jira_search")
    # second write of the SAME rule → look up + reuse, not a new id
    lid = store.find_by_concept("learning", meta, "Always paginate jira_search")
    assert lid == r1["id"]
    r2 = store.save_node("learning", lid, meta, "Always paginate jira_search")
    assert r2["id"] == r1["id"]
    assert len(_learnings(store)) == 1                 # ONE file, not two


def test_paraphrase_matches_by_similarity(store):
    meta = {"scope": "global"}
    store.save_node("learning", None, meta,
                    "Always paginate the jira_search results fully")
    # a close paraphrase resolves to the same concept
    lid = store.find_by_concept(
        "learning", meta, "Always paginate the jira search results in full")
    assert lid is not None


def test_different_concept_gets_new_file(store):
    meta = {"scope": "global"}
    store.save_node("learning", None, meta, "Always paginate jira_search")
    lid = store.find_by_concept("learning", meta,
                                "Restart the api after every deploy")
    assert lid is None                                  # unrelated → new file


def test_scope_isolation(store):
    """Same rule text in a DIFFERENT scope is a different file (global vs
    project) — the ≤2-per-concept (global + project) shape."""
    store.save_node("learning", None, {"scope": "global"}, "Prefer async IO")
    lid = store.find_by_concept(
        "learning", {"workspace": "CacheLayer", "scope": "repo:CacheLayer"},
        "Prefer async IO")
    assert lid is None                                  # project scope ≠ global


def test_dedupe_collapses_fuzzy_duplicates(store):
    meta = {"scope": "global"}
    # simulate the pre-fix pile-up: three near-identical learnings, distinct ids
    store.save_node("learning", "L-01", meta, "Always paginate jira_search calls")
    store.save_node("learning", "L-07", meta, "Always paginate the jira_search calls")
    store.save_node("learning", "L-13", meta, "Always paginate jira_search calls fully")
    store.save_node("learning", "L-20", meta, "Restart the api after deploy")
    assert len(_learnings(store)) == 4
    res = store.dedupe_nodes()
    assert res["ok"] and res["removed"] == 2            # 3 paraphrases → 1
    kinds = _learnings(store)
    assert len(kinds) == 2                              # the concept + the unrelated one
