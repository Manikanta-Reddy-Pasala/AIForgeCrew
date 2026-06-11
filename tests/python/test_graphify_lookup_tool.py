"""Smoke tests for ``aiforge_core.runtime.graphify_lookup_tool``.

The repo's own ``graphify-out/graph.json`` is the fixture. Each test asserts
on the *shape* of the response so the suite stays green even as the graph
itself drifts (new nodes/edges per ingest).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.runtime.graphify_lookup_tool import graphify_lookup

REPO_ROOT = Path(__file__).resolve().parents[2]


def _has_graph() -> bool:
    return (REPO_ROOT / "graphify-out" / "graph.json").is_file()


pytestmark = pytest.mark.skipif(
    not _has_graph(),
    reason="graphify-out/graph.json not present — run `graphify update .` first",
)


def test_label_query_returns_typed_neighbors():
    """A high-degree symbol resolves to matches + edge-typed neighbors."""
    res = graphify_lookup("Memory", hops=1, max_neighbors=10,
                          repo_root=str(REPO_ROOT))
    assert res["ok"] is True
    assert res["matches"], "expected at least one match for 'Memory'"
    assert res["neighbors"], "expected at least one neighbor for 'Memory'"
    sample = res["neighbors"][0]
    assert {"node", "relation", "weight", "confidence",
            "direction", "hop", "source_location"} <= set(sample)
    assert sample["direction"] in ("in", "out")
    assert sample["hop"] in (1, 2)


def test_file_path_query_returns_contained_symbols():
    """Querying by source path lists the file's symbols (contains edges out).

    Uses pipeline.py — a stable, heavily-symbolled file. (The old
    fixture path runtime/memory.py was deleted from the repo; the test
    then failed on graph content, not tool behaviour.)"""
    res = graphify_lookup("aiforge_core/runtime/pipeline.py", hops=1,
                          max_neighbors=8, repo_root=str(REPO_ROOT))
    assert res["ok"] is True
    assert res["matches"], "no matches for known repo file"
    contains_out = [n for n in res["neighbors"]
                    if n["relation"] == "contains" and n["direction"] == "out"]
    assert contains_out, "expected at least one 'contains out' neighbor"


def test_unknown_query_returns_empty_not_error():
    """Garbage queries should resolve to empty lists, not raise/return error."""
    res = graphify_lookup("zzz_not_a_real_symbol_or_file_xxx", hops=1,
                          repo_root=str(REPO_ROOT))
    assert res["ok"] is True
    assert res["matches"] == []
    assert res["neighbors"] == []


def test_invalid_hops_rejected():
    """Hops outside {1, 2} returns ok=False — agent loop stays alive."""
    res = graphify_lookup("Memory", hops=5, repo_root=str(REPO_ROOT))
    assert res["ok"] is False
    assert "hops" in res["error"].lower()


def test_missing_graph_returns_clean_error(tmp_path):
    """Pointing at a directory without graphify-out/ surfaces a clean error."""
    res = graphify_lookup("Memory", repo_root=str(tmp_path))
    assert res["ok"] is False
    assert "graphify" in res["error"].lower()


def test_two_hop_expansion_grows_results():
    """hops=2 should not return fewer neighbors than hops=1 for the same seed."""
    one = graphify_lookup("Memory", hops=1, max_neighbors=50,
                          repo_root=str(REPO_ROOT))
    two = graphify_lookup("Memory", hops=2, max_neighbors=50,
                          repo_root=str(REPO_ROOT))
    assert one["ok"] and two["ok"]
    assert len(two["neighbors"]) >= len(one["neighbors"])
