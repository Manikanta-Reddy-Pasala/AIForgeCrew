"""Unanchored recall hits (keyword rows, linked briefs, repo-less topic briefs)
must be about the question to reach the prompt."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.memory.unified_query import _query as Q


def _ctx(text):
    return SimpleNamespace(text=text)


def test_an_unrelated_keyword_brief_is_dropped_and_a_related_one_kept():
    hits = [
        {"channel": "keyword", "source": "compacted:compacted-tokenizer-rules",
         "text": "Tokenizer must reject consecutive numbers", "score": 0.9},
        {"channel": "keyword", "source": "compacted:compacted-memory-compaction",
         "text": "Compaction folds duplicate facts in memory briefs", "score": 0.5},
    ]
    out = Q._gate_unanchored(_ctx("why does memory compaction keep duplicates"), hits)
    assert [h["source"] for h in out] == ["compacted:compacted-memory-compaction"]


def test_a_repo_scoped_or_cosine_or_pinned_hit_is_not_gated():
    hits = [
        {"channel": "memory", "source": "doer", "repo": "shop", "text": "x"},
        {"channel": "memory", "source": "compacted:compacted-shop", "repo": "shop", "text": "y"},
        {"channel": "constraint", "pinned": True, "text": "never push to prod"},
    ]
    assert Q._gate_unanchored(_ctx("fix the nats retry"), hits) == hits


def test_a_repo_less_topic_brief_from_the_vector_channel_is_gated():
    junk = {"channel": "memory", "source": "compacted:compacted-chatmode", "repo": None,
            "text": "CLI usage: python todo.py add task"}
    assert Q._gate_unanchored(_ctx("fix the NATS push sync retry"), [junk]) == []


def test_the_floor_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_ANCHOR_MIN_OVERLAP", "0")
    h = {"channel": "keyword", "source": "x", "text": "unrelated"}
    assert Q._gate_unanchored(_ctx("memory compaction"), [h]) == [h]


def test_linked_briefs_are_capped(monkeypatch):
    assert Q._linked_cap() == 2
    monkeypatch.setenv("AIFORGE_UMEM_LINKED_MAX", "0")
    assert Q._linked_cap() == 0
