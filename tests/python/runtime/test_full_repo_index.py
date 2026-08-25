"""Full repo index — code + doc chunk layers into the embedded SQLite store.

The old graph layers (tree-sitter symbols + graphify) were removed with the
Neo4j backend; the chunk layers are the whole index now. memory_write is
mocked so the test runs offline.
"""
from __future__ import annotations

import importlib

import pytest


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


def test_chunks_indexed(mi, tmp_path):
    repo = _make_repo(tmp_path)
    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["error"] is None
    assert res["units"] > 0                     # code + doc chunks
    assert res["layers"]["code_chunks"] == "ok"
    assert res["layers"]["doc_chunks"] == "ok"
    # The graph layers were removed — always reported as zero.
    assert res["symbols"] == 0
    assert res["graphify_nodes"] == 0


def test_mixed_code_and_docs_folder(mi, tmp_path):
    """A folder with BOTH .java and .md → code_chunks AND doc_chunks run."""
    repo = _make_repo(tmp_path, py=False)   # only A.java + README.md
    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})

    assert res["layers"]["code_chunks"] == "ok"
    assert res["layers"]["doc_chunks"] == "ok"
    assert res["code_units"] > 0
    assert res["doc_units"] > 0
    refs = [w["tags"][-1] for w in mi._written]
    assert any("A.java" in r for r in refs)
    assert any("README.md" in r for r in refs)


def test_disable_code_chunks_env(mi, monkeypatch, tmp_path):
    repo = _make_repo(tmp_path, java=True, py=True, md=False)
    monkeypatch.setenv("AIFORGE_INDEX_CODE_CHUNKS", "0")
    res = mi.ingest_source({"kind": "repo", "name": "r", "location": str(repo)})
    assert res["layers"]["code_chunks"].startswith("skip")
    assert res["code_units"] == 0
