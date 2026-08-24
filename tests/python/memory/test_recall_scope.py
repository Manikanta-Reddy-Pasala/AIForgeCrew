"""R1 — global ('shared') knowledge must be reachable from a repo-scoped recall.

The recall filter is `repo = ? OR repo IS NULL OR repo = 'shared'`, so a query
scoped to one repo still surfaces global facts (and repo-agnostic rows) but never
another project's rows.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    import aiforge_core.memory.local_embed as le
    monkeypatch.setattr(le, "embed", lambda t: [1.0, 0.0, 0.0])
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


def test_repo_recall_surfaces_shared_and_excludes_other_repo(sm):
    sm.write_unit(text="GLOBAL never commit to main", kind="learning", repo="shared")
    sm.write_unit(text="NULL agnostic fact", kind="learning", repo=None)
    sm.write_unit(text="SVC endpoint detail", kind="learning", repo="svc")
    sm.write_unit(text="OTHER repo secret", kind="learning", repo="other")

    hits = sm.recall("anything", repo="svc", limit=20)
    texts = " | ".join(h.get("text", "") for h in hits)
    assert "GLOBAL never commit to main" in texts   # shared/global surfaced
    assert "NULL agnostic fact" in texts            # repo-agnostic surfaced
    assert "SVC endpoint detail" in texts           # own repo surfaced
    assert "OTHER repo secret" not in texts         # other project excluded
