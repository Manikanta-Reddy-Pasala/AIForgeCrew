"""Workflow registry — WORKFLOW.md parse/search/author + folder creation."""
import os
import pytest


@pytest.fixture
def wf(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("AIFORGE_WORKFLOWS_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib
    from aiforge_core.runtime import workflows
    importlib.reload(workflows)
    return workflows


def test_ensure_dirs_creates_both(wf, tmp_path):
    out = wf.ensure_dirs()
    assert (tmp_path / "skills").is_dir() and (tmp_path / "workflows").is_dir()
    assert "skills" in out and "workflows" in out


def test_write_and_search(wf):
    r = wf.write_workflow("Release cut", "ship a release",
                          "## Steps\n1. tag\n2. push", triggers=["release", "deploy"])
    assert r["ok"] and r["path"].endswith("WORKFLOW.md")
    hits = wf.search("how do we cut a release")
    assert hits and hits[0]["name"] == "Release cut"


def test_write_requires_name_and_body(wf):
    assert wf.write_workflow("", "d", "b")["error"] == "name and body are required"
    assert wf.write_workflow("n", "d", "")["error"] == "name and body are required"


def test_repo_scope_writes_under_repo(wf, tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(repo))
    r = wf.write_workflow("Repo flow", "x", "## body", scope="repo", cwd=str(repo))
    assert r["ok"] and "/.aiforge/workflows/" in r["path"]


def test_handwritten_workflow_picked_up(wf):
    d = wf._global_dir() / "manual"
    d.mkdir(parents=True)
    (d / "WORKFLOW.md").write_text(
        "---\nname: Manual flow\ndescription: dropped by hand\ntriggers: [onboard]\n---\n\n## steps\ndo it")
    hits = wf.search("onboard")
    assert any(h["name"] == "Manual flow" for h in hits)


def test_auto_context_surfaces_relevant(wf):
    wf.write_workflow("Release cut", "ship a release",
                      "## Steps\n1. tag\n2. push", triggers=["release", "deploy"])
    block = wf.auto_context("how do we cut a release")
    assert "RELEVANT WORKFLOWS" in block and "Release cut" in block
    assert wf.auto_context("unrelated quantum chromodynamics") == ""
