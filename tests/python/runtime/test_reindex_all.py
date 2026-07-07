"""Daily re-index sweep: reindex_all re-indexes every repo/docs source so the
chunk/graph layers stay current with the code. Soft-fails per source.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", tempfile.mkdtemp() + "/s.db")
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def test_reindex_all_empty(cfg):
    from aiforge_core.runtime import memory_ingest as mi
    assert mi.reindex_all() == {"total": 0, "indexed": 0, "errors": []}


def test_reindex_all_indexes_repo_sources_only(cfg, monkeypatch):
    from aiforge_core.runtime import memory_ingest as mi
    from aiforge_core.runtime import memory_sources as ms
    ms.create("repo", tempfile.mkdtemp(), "r1")
    ms.create("url", "http://x", "u1")            # non-repo → skipped
    seen = []
    monkeypatch.setattr(mi, "run_index", lambda sid: seen.append(sid))
    out = mi.reindex_all()
    assert out["total"] == 1 and out["indexed"] == 1 and out["errors"] == []
    assert len(seen) == 1                          # only the repo source


def test_reindex_all_soft_fails_per_source(cfg, monkeypatch):
    from aiforge_core.runtime import memory_ingest as mi
    from aiforge_core.runtime import memory_sources as ms
    a = ms.create("repo", tempfile.mkdtemp(), "a")
    ms.create("repo", tempfile.mkdtemp(), "b")

    def _run(sid):
        if sid == a["id"]:
            raise RuntimeError("boom")
    monkeypatch.setattr(mi, "run_index", _run)
    out = mi.reindex_all()
    assert out["total"] == 2 and out["indexed"] == 1     # the other still ran
    assert len(out["errors"]) == 1 and out["errors"][0]["id"] == a["id"]
