"""Ranking/diversification behaviour of unified_query.

Split out of the old test_unified_query_afm.py when the Neo4j-backed
AiForgeMemory bundle and cross-repo sources were removed with the graph layer.
Those 19 tests went with the code they covered; these 6 cover the generic
per-group diversification that is still live, so they were kept rather than
deleted along with the file.
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture
def uq():
    """Fresh import per test + cleanup so test_doer_tools' sys.modules
    monkeypatch isolation isn't broken by this file caching the module
    (parent-package attribute lookup wins over sys.modules.setitem)."""
    sys.modules.pop("aiforge_core.memory.unified_query", None)
    import aiforge_core.memory as _mem
    if hasattr(_mem, "unified_query"):
        delattr(_mem, "unified_query")
    from aiforge_core.memory import unified_query
    yield unified_query
    sys.modules.pop("aiforge_core.memory.unified_query", None)
    if hasattr(_mem, "unified_query"):
        delattr(_mem, "unified_query")


def test_diversify_caps_per_group(uq) -> None:
    hits = [{"source": "memory", "ticket": "ONE-1", "text": str(i)}
            for i in range(5)]
    hits += [{"source": "doc", "text": "d1"},
             {"source": "doc", "text": "d2"}]
    out = uq._diversify(hits, per_group=3)
    # 3 kept from the ONE-1 flood + both doc rows = 5.
    assert len(out) == 5
    one1 = [h for h in out if h.get("ticket") == "ONE-1"]
    assert len(one1) == 3
    # highest-ranked survivors kept, order preserved.
    assert [h["text"] for h in one1] == ["0", "1", "2"]


def test_diversify_keys_by_source_without_ticket(uq) -> None:
    # Two distinct sources so the single-source cap-skip doesn't apply;
    # each source is still capped at per_group.
    hits = [{"source": "doc", "text": str(i)} for i in range(4)]
    hits += [{"source": "memory", "text": f"m{i}"} for i in range(4)]
    out = uq._diversify(hits, per_group=2)
    assert len(out) == 4
    assert [h["text"] for h in out if h["source"] == "doc"] == ["0", "1"]


def test_diversify_single_source_skips_cap(uq) -> None:
    # All hits collapse to ONE group (source="doc") → cap is skipped so an
    # embedded-SQLite-style single-source recall isn't squashed to per_group.
    hits = [{"source": "doc", "text": str(i)} for i in range(4)]
    out = uq._diversify(hits, per_group=2)
    assert len(out) == 4
    assert [h["text"] for h in out] == ["0", "1", "2", "3"]


def test_diversify_passthrough_under_cap(uq) -> None:
    hits = [{"source": "memory", "text": "a"},
            {"source": "doc", "text": "b"}]
    out = uq._diversify(hits, per_group=3)
    assert out == hits


def test_diversify_disabled_when_per_group_zero(uq) -> None:
    hits = [{"source": "memory", "ticket": "ONE-1", "text": str(i)}
            for i in range(5)]
    out = uq._diversify(hits, per_group=0)
    assert out == hits


# ── Gap A7: cross-repo CALLS_REPO neighbour source ──────────────────
