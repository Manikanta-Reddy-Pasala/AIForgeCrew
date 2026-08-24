"""Full multi-layer repo index — chunks (code + docs) + tree-sitter symbols
+ graphify. Every layer soft-fails independently; chunks are the baseline.

Neo4j / graphify / tree-sitter are all mocked so the test runs offline.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest


class _FakeStats:
    """Stand-in for treesitter_ingest.IngestStats."""

    def as_dict(self) -> dict:
        return {"files_seen": 3, "symbols_written": 7}


def _make_repo(tmp_path, *, java=True, py=True, md=True):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    if java:
        (repo / "src" / "A.java").write_text(
            "package p;\npublic class A {\n  int f(){ return 1; }\n}\n" * 20)
    if py:
        (repo / "src" / "a.py").write_text("def f():\n    return 1\n" * 40)
    if md:
        (repo / "README.md").write_text("# Title\n" + "doc line\n" * 40)
    return repo


@pytest.fixture
def mi(monkeypatch):
    """memory_ingest with memory_write captured and sqlite backend."""
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    import aiforge_core.runtime.tools.memory_write as mw
    written: list = []
    monkeypatch.setattr(
        mw, "memory_write",
        lambda **kw: written.append(kw) or {"ok": True, "id": len(written)})
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    mi._written = written  # type: ignore[attr-defined]
    return mi


def _wire_all_layers(monkeypatch, mi, tmp_path):
    """Monkeypatch the three graph layers so they all 'succeed'."""
    fake_driver = MagicMock(name="neo4j_driver")
    monkeypatch.setattr(mi, "_neo4j_driver_or_none", lambda: fake_driver)

    import aiforge_core.indexing.treesitter_ingest as tsi
    monkeypatch.setattr(tsi, "TREESITTER_AVAILABLE", True)
    ts_mock = MagicMock(return_value=_FakeStats())
    monkeypatch.setattr(tsi, "ingest_repo", ts_mock)

    import aiforge_core.indexing.graphify_loader as gl
    gl_mock = MagicMock(return_value={"nodes_created": 5})
    monkeypatch.setattr(gl, "load_graphify_json", gl_mock)

    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/graphify")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: MagicMock(returncode=0))
    return fake_driver, ts_mock, gl_mock


def test_all_layers_invoked(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    # graphify-out/graph.json must exist for the loader to be called
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "graph.json").write_text('{"nodes":[],"links":[]}')
    _, ts_mock, gl_mock = _wire_all_layers(monkeypatch, mi, tmp_path)

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["error"] is None
    assert res["units"] > 0                     # code + doc chunks
    assert res["symbols"] == 7
    assert res["graphify_nodes"] == 5
    layers = res["layers"]
    assert layers["code_chunks"] == "ok"
    assert layers["doc_chunks"] == "ok"
    assert layers["symbols"] == "ok"
    assert layers["graphify"] == "ok"
    ts_mock.assert_called_once()
    gl_mock.assert_called_once()


def test_mixed_code_and_docs_folder(mi, monkeypatch, tmp_path):
    """A folder with BOTH .java and .md → code_chunks AND doc_chunks run,
    symbols invoked for the java."""
    repo = _make_repo(tmp_path, py=False)   # only A.java + README.md
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "graph.json").write_text('{"nodes":[],"links":[]}')
    _, ts_mock, _ = _wire_all_layers(monkeypatch, mi, tmp_path)

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["layers"]["code_chunks"] == "ok"
    assert res["layers"]["doc_chunks"] == "ok"
    assert res["code_units"] > 0
    assert res["doc_units"] > 0
    ts_mock.assert_called_once()                # symbols for the java file
    refs = [w["tags"][-1] for w in mi._written]
    assert any("A.java" in r for r in refs)
    assert any("README.md" in r for r in refs)


def test_neo4j_unconfigured_chunks_only(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mi, "_neo4j_driver_or_none", lambda: None)

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["error"] is None
    assert res["units"] > 0
    assert res["layers"]["code_chunks"] == "ok"
    assert res["layers"]["doc_chunks"] == "ok"
    assert res["layers"]["symbols"].startswith("skip:")
    assert res["layers"]["graphify"].startswith("skip:")
    assert res["symbols"] == 0
    assert res["graphify_nodes"] == 0


def test_graphify_cli_absent(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    fake_driver = MagicMock()
    monkeypatch.setattr(mi, "_neo4j_driver_or_none", lambda: fake_driver)
    import aiforge_core.indexing.treesitter_ingest as tsi
    monkeypatch.setattr(tsi, "TREESITTER_AVAILABLE", True)
    monkeypatch.setattr(tsi, "ingest_repo", MagicMock(return_value=_FakeStats()))
    import shutil
    monkeypatch.delenv("AIFORGE_GRAPHIFY_BIN", raising=False)  # no override
    monkeypatch.setattr(shutil, "which", lambda name: None)   # no graphify CLI

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["layers"]["graphify"].startswith("skip:graphify_cli_absent")
    assert res["layers"]["symbols"] == "ok"
    assert res["layers"]["code_chunks"] == "ok"
    assert res["error"] is None


def test_symbol_layer_exception_isolated(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    fake_driver = MagicMock()
    monkeypatch.setattr(mi, "_neo4j_driver_or_none", lambda: fake_driver)
    import aiforge_core.indexing.treesitter_ingest as tsi
    monkeypatch.setattr(tsi, "TREESITTER_AVAILABLE", True)

    def _boom(*a, **k):
        raise RuntimeError("ts blew up")
    monkeypatch.setattr(tsi, "ingest_repo", _boom)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)  # graphify skip

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["layers"]["symbols"].startswith("error:")
    assert res["layers"]["code_chunks"] == "ok"     # unaffected
    assert res["layers"]["doc_chunks"] == "ok"      # unaffected
    assert res["error"] is None                     # overall still ok
    assert res["units"] > 0


def test_disable_symbols_env(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("AIFORGE_INDEX_SYMBOLS", "0")
    ts_mock = MagicMock()
    import aiforge_core.indexing.treesitter_ingest as tsi
    monkeypatch.setattr(tsi, "ingest_repo", ts_mock)
    monkeypatch.setattr(tsi, "TREESITTER_AVAILABLE", True)
    monkeypatch.setattr(mi, "_neo4j_driver_or_none", lambda: MagicMock())
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)

    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["layers"]["symbols"] == "skip:disabled"
    ts_mock.assert_not_called()
    assert res["layers"]["code_chunks"] == "ok"
