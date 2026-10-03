"""Chat recall across projects: own project first, other projects only when
clearly relevant and only a few.

The scoped recall (own project + global) is unchanged. A chat recall adds one
more source that looks outside the chat's scope, gated by a relevance floor
and a cap so an unrelated project cannot crowd out the chat's own memory.
Ticket and pipeline recalls do not get it at all.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def uq(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    for k in ("AIFORGE_UMEM_RECENT", "AIFORGE_UMEM_CHAT",
              "AIFORGE_UMEM_CONSTRAINTS", "AIFORGE_UMEM_LINK_EXPAND"):
        monkeypatch.setenv(k, "0")
    from aiforge_core.memory import backend_select, sqlite_memory
    from aiforge_core.memory import unified_query as _uq
    monkeypatch.setattr(backend_select, "embedded", lambda: True)
    for name in ("_mcp_call", "_docs_lookup"):
        monkeypatch.setattr(_uq, name, lambda *_a, **_k: [])
    monkeypatch.setattr(_uq, "_guess_library", lambda _t: None)
    monkeypatch.setattr(_uq, "_rerank_top", lambda hits, query: None)
    monkeypatch.setattr(sqlite_memory, "keyword_search", lambda *_a, **_k: [])
    return _uq, sqlite_memory


def _row(text, repo, score, rid):
    return {"text": text, "title": text[:20], "source": "doer",
            "group": f"sqlite:{rid}", "kind": "learning", "ticket": None,
            "repo": repo, "score": score}


def _stub(monkeypatch, sqlite_memory, scoped, everything):
    def _recall(text, *, limit=8, repo=None, boost_tags=None):
        return list(everything if repo is None else scoped)[:limit]
    monkeypatch.setattr(sqlite_memory, "recall", _recall)


def test_other_project_is_offered_labelled_and_below_own(uq, monkeypatch):
    _uq, sm = uq
    own = _row("shop retries payments three times", "shop", 0.70, 1)
    glob = _row("always squash commits", "shared", 0.70, 2)
    other = _row("billing retries payments five times", "billing", 0.80, 3)
    _stub(monkeypatch, sm, [own, glob], [other, own, glob])
    hits = _uq.query("payment retries", repo="shop", cross_project=True)["hits"]
    texts = [h["text"] for h in hits]
    assert texts[0] == own["text"]                       # own project first
    cross = next(h for h in hits if h["text"] == other["text"])
    assert cross["project"] == "billing" and cross["channel"] == "cross"
    assert texts.index(own["text"]) < texts.index(other["text"])


def test_own_project_outranks_an_equally_relevant_global_fact(uq, monkeypatch):
    _uq, sm = uq
    own = _row("shop deploys on Fridays", "shop", 0.60, 1)
    glob = _row("deploys need a ticket", "shared", 0.60, 2)
    _stub(monkeypatch, sm, [glob, own], [glob, own])
    hits = _uq.query("deploys", repo="shop", cross_project=True)["hits"]
    assert [h["text"] for h in hits][0] == own["text"]


def test_weak_or_excess_outside_rows_are_dropped(uq, monkeypatch):
    _uq, sm = uq
    own = _row("shop fact", "shop", 0.70, 1)
    outside = [_row(f"billing fact {i}", "billing", s, 10 + i)
               for i, s in enumerate((0.90, 0.85, 0.80, 0.30))]
    _stub(monkeypatch, sm, [own], outside + [own])
    hits = _uq.query("fact", repo="shop", cross_project=True)["hits"]
    cross = [h["text"] for h in hits if h.get("project")]
    assert cross == ["billing fact 0", "billing fact 1"]   # cap 2, floor drops 0.30


def test_floor_and_cap_are_settings(uq, monkeypatch):
    _uq, sm = uq
    monkeypatch.setenv("AIFORGE_UMEM_CROSS_MAX", "1")
    monkeypatch.setenv("AIFORGE_UMEM_CROSS_MIN_SCORE", "0.88")
    outside = [_row("billing a", "billing", 0.90, 1),
               _row("billing b", "billing", 0.85, 2)]
    _stub(monkeypatch, sm, [], outside)
    hits = _uq.query("billing", repo="shop", cross_project=True)["hits"]
    assert [h["text"] for h in hits if h.get("project")] == ["billing a"]
    monkeypatch.setenv("AIFORGE_UMEM_CROSS_PROJECT", "0")
    hits = _uq.query("billing", repo="shop", cross_project=True)["hits"]
    assert not any(h.get("project") for h in hits)


def test_ticket_and_pipeline_recall_never_look_outside(uq, monkeypatch):
    _uq, sm = uq
    seen: list = []

    def _recall(text, *, limit=8, repo=None, boost_tags=None):
        seen.append(repo)
        return [_row("billing fact", "billing", 0.9, 1)] if repo is None else []
    monkeypatch.setattr(sm, "recall", _recall)
    hits = _uq.query("fact", repo="shop")["hits"]          # cross_project off
    assert hits == [] and seen == ["shop"]


def test_a_chat_with_no_project_reads_every_project(uq, monkeypatch):
    _uq, sm = uq
    gen = _row("general note about retries", "general", 0.60, 1)
    shop = _row("shop retries payments three times", "shop", 0.80, 2)
    _stub(monkeypatch, sm, [gen], [shop, gen])
    hits = _uq.query("retries", repo="general", cross_project=True)["hits"]
    assert {h["text"] for h in hits} == {gen["text"], shop["text"]}
    assert next(h for h in hits if h["text"] == shop["text"])["project"] == "shop"


def test_note_and_rule_buckets_stay_out_and_general_is_not_named(uq, monkeypatch):
    _uq, sm = uq
    rows = [_row("a session summary", "notes", 0.95, 1),
            _row("a rule book line", "rules", 0.94, 2),
            _row("general chat fact about retries", "general", 0.90, 3),
            _row("billing retries five times", "billing", 0.85, 4)]
    _stub(monkeypatch, sm, [], rows)
    hits = _uq.query("retries", repo="shop", cross_project=True)["hits"]
    by = {h["text"]: h for h in hits}
    assert set(by) == {"general chat fact about retries",
                       "billing retries five times"}
    assert "project" not in by["general chat fact about retries"]
    assert by["billing retries five times"]["project"] == "billing"


def test_project_label_reaches_the_prompt():
    from aiforge_core.runtime.chat_agent._context._recall import _ranked_lines
    out = _ranked_lines([{"text": "billing retries five times",
                          "source": "doer", "project": "billing"},
                         {"text": "shop retries three times", "source": "doer"}], 5)
    assert "(project billing: from another project" in out
    assert "do not apply to this one, doer)" in out
    assert "shop retries three times  (doer)" in out
