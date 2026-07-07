"""capture → md (repo+topic stamped) → compact by repo/topic → load brief.
The user requirement: everything must be WRITTEN to md, else it never reaches
compaction or the loadable project brief.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def test_capture_writes_md_with_repo_and_topic(cfg):
    from aiforge_core.memory import md_store as m
    d = m.capture("project_learning", "db access only via gateway",
                  repo="svc", topic="architecture")
    assert d["repo"] == "svc"
    assert d["topic"] == "architecture"
    assert "repo:svc" in d["tags"] and "topic:architecture" in d["tags"]


def test_compact_by_repo_makes_project_brief(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "svc: validate at boundary", repo="svc", topic="a")
    m.capture("project_learning", "svc: no direct mongo", repo="svc", topic="b")
    m.capture("project_learning", "other: unrelated", repo="other", topic="a")
    r = m.compact(group_by="repo", min_group=2, summarize=False)
    assert r["groups"].get("svc") == 2          # svc rolled up
    assert "other" not in r["groups"]           # singleton left alone
    briefs = sorted(p.name for p in m.memory_dir().glob("compacted-svc.md"))
    assert briefs == ["compacted-svc.md"]


def test_compact_by_topic_groups_cross_repo(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("topic_learning", "auth: rotate keys 90d", repo="a", topic="auth")
    m.capture("topic_learning", "auth: mTLS between services", repo="b", topic="auth")
    r = m.compact(group_by="topic", min_group=2, summarize=False)
    assert r["groups"].get("auth") == 2         # cross-repo topic rolled up


def test_project_brief_loads_for_repo(cfg):
    import subprocess
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import context_bundle as cb
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo])
    rn = os.path.basename(repo)
    m.capture("project_learning", f"{rn}: rule one", repo=rn, topic="x")
    m.capture("project_learning", f"{rn}: rule two", repo=rn, topic="y")
    m.compact(group_by="repo", min_group=2, summarize=False)
    brief = cb._project_brief(repo)
    assert brief.startswith("PROJECT MEMORY (")
    assert "rule one" in brief or "rule two" in brief
