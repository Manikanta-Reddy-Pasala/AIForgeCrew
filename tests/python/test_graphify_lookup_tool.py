"""Tests for ``aiforge_core.runtime.graphify_lookup_tool``.

The fixture graph is SYNTHETIC and written into ``tmp_path`` — the tool's
behaviour is what's under test, not the repo's own ``graphify-out/``
(which is gitignored, so it does not exist in a fresh clone/CI image and
drifts on every ingest when it does).
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.graphify_lookup_tool import graphify_lookup

# ─── synthetic graph ────────────────────────────────────────────────────
#
#   pipeline.py ──contains──▶ run_pipeline ──calls──▶ Memory
#                                                       │uses
#                                                       ▼
#                                                   Embedder ──uses──▶ Tokenizer
#
# Memory therefore has one IN edge (calls) and one OUT edge (uses) at hop 1,
# and picks up pipeline.py + Tokenizer at hop 2.

_NODES = [
    {"id": "n_file_pipeline", "label": "pipeline.py",
     "source_file": "pkg/pipeline.py", "source_location": "pkg/pipeline.py",
     "community": 0, "file_type": "python"},
    {"id": "n_run_pipeline", "label": "run_pipeline",
     "source_file": "pkg/pipeline.py", "source_location": "pkg/pipeline.py:12",
     "community": 0, "file_type": "python"},
    {"id": "n_memory", "label": "Memory",
     "source_file": "pkg/memory.py", "source_location": "pkg/memory.py:20",
     "community": 1, "file_type": "python"},
    {"id": "n_embedder", "label": "Embedder",
     "source_file": "pkg/embed.py", "source_location": "pkg/embed.py:5",
     "community": 1, "file_type": "python"},
    {"id": "n_tokenizer", "label": "Tokenizer",
     "source_file": "pkg/embed.py", "source_location": "pkg/embed.py:40",
     "community": 1, "file_type": "python"},
]

_LINKS = [
    {"source": "n_file_pipeline", "target": "n_run_pipeline",
     "relation": "contains", "weight": 1.0, "confidence": "high",
     "source_location": "pkg/pipeline.py:12"},
    {"source": "n_run_pipeline", "target": "n_memory",
     "relation": "calls", "weight": 2.0, "confidence": "high",
     "source_location": "pkg/pipeline.py:30"},
    {"source": "n_memory", "target": "n_embedder",
     "relation": "uses", "weight": 1.0, "confidence": "medium",
     "source_location": "pkg/memory.py:44"},
    {"source": "n_embedder", "target": "n_tokenizer",
     "relation": "uses", "weight": 1.0, "confidence": "medium",
     "source_location": "pkg/embed.py:9"},
]


@pytest.fixture()
def graph_root(tmp_path):
    """A repo root whose ``graphify-out/graph.json`` is the synthetic graph."""
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({"nodes": _NODES, "links": _LINKS}))
    return str(tmp_path)


def test_label_query_returns_typed_neighbors(graph_root):
    """A symbol label resolves to matches + edge-typed neighbors."""
    res = graphify_lookup("Memory", hops=1, max_neighbors=10, repo_root=graph_root)
    assert res["ok"] is True
    assert [m["id"] for m in res["matches"]] == ["n_memory"]
    assert res["neighbors"], "expected at least one neighbor for 'Memory'"
    sample = res["neighbors"][0]
    assert {"node", "relation", "weight", "confidence",
            "direction", "hop", "source_location"} <= set(sample)
    assert sample["direction"] in ("in", "out")
    assert sample["hop"] in (1, 2)
    # both directions are reachable from a single seed
    by_rel = {(n["relation"], n["direction"]) for n in res["neighbors"]}
    assert ("calls", "in") in by_rel
    assert ("uses", "out") in by_rel


def test_file_path_query_returns_contained_symbols(graph_root):
    """Querying by source path lists the file's symbols (contains edges out)."""
    res = graphify_lookup("pkg/pipeline.py", hops=1, max_neighbors=8,
                          repo_root=graph_root)
    assert res["ok"] is True
    assert res["matches"], "no matches for known repo file"
    contains_out = [n for n in res["neighbors"]
                    if n["relation"] == "contains" and n["direction"] == "out"]
    assert contains_out, "expected at least one 'contains out' neighbor"
    assert contains_out[0]["node"]["label"] == "run_pipeline"


def test_unknown_query_returns_empty_not_error(graph_root):
    """Garbage queries should resolve to empty lists, not raise/return error."""
    res = graphify_lookup("zzz_not_a_real_symbol_or_file_xxx", hops=1,
                          repo_root=graph_root)
    assert res["ok"] is True
    assert res["matches"] == []
    assert res["neighbors"] == []


@pytest.mark.parametrize("hops", [0, 3, 5])
def test_invalid_hops_rejected(graph_root, hops):
    """Hops outside {1, 2} returns ok=False — agent loop stays alive."""
    res = graphify_lookup("Memory", hops=hops, repo_root=graph_root)
    assert res["ok"] is False
    assert "hops" in res["error"].lower()


def test_invalid_hops_rejected_before_the_graph_is_read(tmp_path):
    """Argument validation runs first: a bad ``hops`` is reported as a bad
    ``hops``, not masked by whatever the graph load would have said."""
    res = graphify_lookup("Memory", hops=5, repo_root=str(tmp_path))
    assert res["ok"] is False
    assert "hops" in res["error"].lower()


def test_missing_graph_returns_clean_error(tmp_path):
    """Pointing at a directory without graphify-out/ surfaces a clean error."""
    res = graphify_lookup("Memory", repo_root=str(tmp_path))
    assert res["ok"] is False
    assert "graphify" in res["error"].lower()


def test_two_hop_expansion_grows_results(graph_root):
    """hops=2 should not return fewer neighbors than hops=1 for the same seed."""
    one = graphify_lookup("Memory", hops=1, max_neighbors=50, repo_root=graph_root)
    two = graphify_lookup("Memory", hops=2, max_neighbors=50, repo_root=graph_root)
    assert one["ok"]
    assert two["ok"]
    assert len(two["neighbors"]) > len(one["neighbors"])
    assert any(n["hop"] == 2 for n in two["neighbors"])
