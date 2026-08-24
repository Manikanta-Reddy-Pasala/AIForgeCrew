"""Unit tests for ``aiforge_core.indexing.graphify_loader``.

These tests use a hand-rolled fake Neo4j driver that records every
``session.run(cypher, **params)`` call. We verify:

* the loader parses a synthetic NetworkX-format ``graph.json``
* idempotency: re-running the loader against the same data adds zero
  new node identities (we simulate ``MERGE`` by deduping on key)
* mixed-source coexistence: a :File pre-tagged ``source="treesitter"``
  ends up with ``sources = ["treesitter", "graphify"]`` (Cypher logic
  itself isn't executed here; we instead assert that the loader sends
  ``source_tag="graphify"`` and that the Cypher template contains the
  list-merge ``CASE`` so the runtime behaviour is correct).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiforge_core.indexing import graphify_loader as loader_mod
from aiforge_core.indexing.graphify_loader import (
    CYPHER_FILE,
    CYPHER_SYMBOL,
    _classify_node,
    _line_from_location,
    _relation_type,
    load_graphify_json,
)


# --- fake Neo4j driver ---------------------------------------------------

class _FakeSession:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc) -> None:  # noqa: D401
        return None

    def run(self, cypher: str, **params: Any) -> Any:
        # Record schema vs data calls separately so we can assert later.
        if "rows" in params:
            rows = params["rows"]
            tag = params.get("source_tag", "?")
            kind = "FILE" if "MERGE (f:File" in cypher else (
                "SYMBOL" if "MERGE (s:Symbol" in cypher else (
                    "OTHER" if "MERGE (n:GraphifyNode" in cypher else "EDGE"
                )
            )
            self.store.setdefault("calls", []).append(
                {"kind": kind, "rows": rows, "source_tag": tag, "cypher": cypher}
            )
            # Simulate MERGE: track unique identities.
            for r in rows:
                if kind == "FILE":
                    self.store.setdefault("files", set()).add(r["path"])
                elif kind == "SYMBOL":
                    self.store.setdefault("symbols", set()).add(r["id"])
                elif kind == "OTHER":
                    self.store.setdefault("others", set()).add(r["id"])
                else:
                    self.store.setdefault("edges", set()).add(
                        (r["src"], r["tgt"], cypher.split("MERGE (a)-[rel:")[1].split("]")[0])
                    )
        else:
            self.store.setdefault("schema", []).append(cypher)
        return []


class _FakeDriver:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def session(self) -> _FakeSession:
        return _FakeSession(self.store)

    def close(self) -> None:  # pragma: no cover
        return None


# --- fixtures ------------------------------------------------------------

@pytest.fixture
def synth_graph(tmp_path: Path) -> Path:
    """Synthetic 5-node, 4-edge Graphify graph mirroring the real schema.

    Layout:

        file_a.py  ──contains──► func_a()
                                    │
                                    │ calls
                                    ▼
        file_b.py  ──contains──► func_b()
                                    ▲
                            rationale_for│
                       rationale_node ───┘
    """
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "users_x_repo_file_a_py",
                "label": "file_a.py",
                "file_type": "code",
                "source_file": "/x/repo/file_a.py",
                "source_location": "L1",
                "community": 1,
                "norm_label": "file_a.py",
            },
            {
                "id": "file_a_func_a",
                "label": ".func_a()",
                "file_type": "code",
                "source_file": "/x/repo/file_a.py",
                "source_location": "L10",
                "community": 1,
                "norm_label": ".func_a()",
            },
            {
                "id": "users_x_repo_file_b_py",
                "label": "file_b.py",
                "file_type": "code",
                "source_file": "/x/repo/file_b.py",
                "source_location": "L1",
                "community": 2,
                "norm_label": "file_b.py",
            },
            {
                "id": "file_b_func_b",
                "label": ".func_b()",
                "file_type": "code",
                "source_file": "/x/repo/file_b.py",
                "source_location": "L20",
                "community": 2,
                "norm_label": ".func_b()",
            },
            {
                "id": "rationale_42",
                "label": "Funcs collaborate to do the thing",
                "file_type": "rationale",
                "source_file": "/x/repo/file_b.py",
                "source_location": "L20",
                "community": 2,
                "norm_label": "funcs collaborate to do the thing",
            },
        ],
        "links": [
            {
                "source": "users_x_repo_file_a_py",
                "target": "file_a_func_a",
                "relation": "contains",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": "/x/repo/file_a.py",
                "source_location": "L10",
                "weight": 1.0,
            },
            {
                "source": "users_x_repo_file_b_py",
                "target": "file_b_func_b",
                "relation": "contains",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": "/x/repo/file_b.py",
                "source_location": "L20",
                "weight": 1.0,
            },
            {
                "source": "file_a_func_a",
                "target": "file_b_func_b",
                "relation": "calls",
                "confidence": "INFERRED",
                "confidence_score": 0.8,
                "source_file": "/x/repo/file_a.py",
                "source_location": "L12",
                "weight": 1.0,
            },
            {
                "source": "rationale_42",
                "target": "file_b_func_b",
                "relation": "rationale_for",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": "/x/repo/file_b.py",
                "source_location": "L20",
                "weight": 1.0,
            },
        ],
        "hyperedges": [],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph))
    return p


# --- tiny helper tests ---------------------------------------------------

def test_classify_node_file_vs_symbol() -> None:
    file_node = {
        "label": "file_a.py", "file_type": "code", "source_location": "L1",
    }
    sym_node = {
        "label": ".func_a()", "file_type": "code", "source_location": "L10",
    }
    cls_node = {
        "label": "MyClass", "file_type": "code", "source_location": "L5",
    }
    rationale = {
        "label": "why", "file_type": "rationale", "source_location": "L1",
    }
    assert _classify_node(file_node) == "file"
    assert _classify_node(sym_node) == "symbol"
    assert _classify_node(cls_node) == "symbol"
    assert _classify_node(rationale) == "rationale"


def test_relation_type_canonicalises() -> None:
    assert _relation_type("calls") == "CALLS"
    assert _relation_type("rationale_for") == "RATIONALE_FOR"
    assert _relation_type("semantically_similar_to") == "SEMANTICALLY_SIMILAR_TO"
    assert _relation_type("Imports") == "IMPORTS"
    # Unknown relation gets sanitised + uppercased.
    assert _relation_type("weird-rel.kind") == "WEIRD_REL_KIND"
    assert _relation_type(None) == "RELATED"


def test_line_from_location() -> None:
    assert _line_from_location("L42") == 42
    assert _line_from_location("L30-50") == 30
    assert _line_from_location("L1") == 1
    assert _line_from_location(None) is None
    assert _line_from_location("garbage") is None


# --- dry-run path --------------------------------------------------------

def test_dry_run_classifies_without_writes(synth_graph: Path) -> None:
    stats = load_graphify_json(
        driver=None,
        graph_json_path=synth_graph,
        repo_name="testrepo",
        dry_run=True,
    )
    assert stats["files"] == 2
    assert stats["symbols"] == 2
    assert stats["others"] == 1  # the rationale
    assert stats["nodes_skipped"] == 0
    assert stats["edges_skipped"] == 0
    assert stats["nodes_created"] == 0  # dry-run never writes
    assert stats["edges_created"] == 0
    assert stats["edge_types"] == {
        "CONTAINS": 2, "CALLS": 1, "RATIONALE_FOR": 1,
    }
    assert stats["repo"] == "testrepo"
    assert stats["source_tag"] == "graphify"


def test_dry_run_requires_no_driver_and_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        load_graphify_json(
            driver=None, graph_json_path=missing,
            repo_name="x", dry_run=True,
        )


# --- fake-driver write path ---------------------------------------------

def test_load_creates_expected_buckets(synth_graph: Path) -> None:
    drv = _FakeDriver()
    stats = load_graphify_json(
        driver=drv,
        graph_json_path=synth_graph,
        repo_name="testrepo",
        dry_run=False,
    )
    assert stats["nodes_created"] == 5  # 2 files + 2 symbols + 1 other
    assert stats["edges_created"] == 4
    # Files MERGEd by path.
    assert drv.store["files"] == {"/x/repo/file_a.py", "/x/repo/file_b.py"}
    # Symbols MERGEd by graphify id.
    assert drv.store["symbols"] == {"file_a_func_a", "file_b_func_b"}
    # Catch-all rationale node lands in :GraphifyNode.
    assert drv.store["others"] == {"rationale_42"}
    # All edge types created.
    edge_types = {t for _, _, t in drv.store["edges"]}
    assert edge_types == {"CONTAINS", "CALLS", "RATIONALE_FOR"}


def test_idempotent_double_load_zero_new_identities(synth_graph: Path) -> None:
    drv = _FakeDriver()
    load_graphify_json(driver=drv, graph_json_path=synth_graph,
                       repo_name="testrepo", dry_run=False)
    files_after_first = set(drv.store["files"])
    symbols_after_first = set(drv.store["symbols"])
    edges_after_first = set(drv.store["edges"])

    # Second run: simulating MERGE, no new identities should appear in our
    # fake's `set`-based tracking.
    load_graphify_json(driver=drv, graph_json_path=synth_graph,
                       repo_name="testrepo", dry_run=False)
    assert drv.store["files"] == files_after_first
    assert drv.store["symbols"] == symbols_after_first
    assert drv.store["edges"] == edges_after_first


def test_every_call_carries_source_tag(synth_graph: Path) -> None:
    drv = _FakeDriver()
    load_graphify_json(
        driver=drv, graph_json_path=synth_graph,
        repo_name="testrepo", dry_run=False, source_tag="graphify",
    )
    data_calls = [c for c in drv.store["calls"]]
    assert data_calls, "expected at least one data write"
    for c in data_calls:
        assert c["source_tag"] == "graphify", c


def test_custom_source_tag_propagates(synth_graph: Path) -> None:
    drv = _FakeDriver()
    load_graphify_json(
        driver=drv, graph_json_path=synth_graph,
        repo_name="testrepo", dry_run=False,
        source_tag="graphify-experiment",
    )
    for c in drv.store["calls"]:
        assert c["source_tag"] == "graphify-experiment"


# --- Cypher template assertions -----------------------------------------
#
# We can't execute Cypher in unit tests, but we can assert the templates
# encode the multi-source merge semantics: when an existing node already
# has source="treesitter" (and no `sources` list yet), the loader must
# extend it to `["treesitter", "graphify"]` instead of clobbering.

def test_file_cypher_handles_legacy_treesitter_source() -> None:
    # Legacy nodes (from scip_to_neo4j ingest) have `source` (scalar) but
    # no `sources` list. The loader's CASE block must lift them.
    assert "f.sources IS NULL AND f.source IS NULL" in CYPHER_FILE
    assert "WHEN f.sources IS NULL THEN" in CYPHER_FILE
    assert "f.source = $source_tag" in CYPHER_FILE
    # Idempotent: same tag re-applied must not append a duplicate.
    assert "$source_tag IN f.sources THEN f.sources" in CYPHER_FILE


def test_symbol_cypher_handles_legacy_treesitter_source() -> None:
    assert "s.sources IS NULL AND s.source IS NULL" in CYPHER_SYMBOL
    assert "WHEN s.sources IS NULL THEN" in CYPHER_SYMBOL
    assert "$source_tag IN s.sources THEN s.sources" in CYPHER_SYMBOL


# --- malformed input -----------------------------------------------------

def test_skips_nodes_without_id_and_edges_without_endpoints(tmp_path: Path) -> None:
    bad = {
        "nodes": [
            {"label": "no-id"},
            {"id": "good", "label": "good.py", "file_type": "code",
             "source_file": "/p/good.py", "source_location": "L1"},
            {"id": "good", "label": "dup"},  # duplicate id
        ],
        "links": [
            {"source": None, "target": "good", "relation": "calls"},
            {"source": "good", "target": None, "relation": "calls"},
            {"source": "good", "target": "good", "relation": "calls"},
        ],
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    drv = _FakeDriver()
    stats = load_graphify_json(driver=drv, graph_json_path=p,
                               repo_name="r", dry_run=False)
    assert stats["nodes_skipped"] == 2  # no-id + duplicate
    assert stats["edges_skipped"] == 2  # the two with None endpoints
    assert stats["edges_created"] == 1


def test_load_requires_driver_when_not_dry_run(synth_graph: Path) -> None:
    with pytest.raises(ValueError):
        load_graphify_json(driver=None, graph_json_path=synth_graph,
                           repo_name="r", dry_run=False)
