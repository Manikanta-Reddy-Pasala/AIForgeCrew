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


def test_ensure_dirs_surfaces_default_playbooks(wf, monkeypatch, tmp_path):
    from aiforge_core.runtime import skills as sk
    # ensure_dirs no longer COPIES builtins into the global dir — load() reads
    # the shipped defaults directly (low-priority), so the defaults are visible
    # without any seeding and the global dir stays empty of default copies.
    res = wf.ensure_dirs()
    assert (tmp_path / "skills").is_dir() and (tmp_path / "workflows").is_dir()
    assert "skills" in res and "workflows" in res
    sk_names = {s.name for s in sk.load()}
    wf_names = {w.name for w in wf.load()}
    assert len(sk_names) >= 3 and len(wf_names) >= 3
    assert {"systematic-debugging", "test-driven-development",
            "safe-refactoring"} <= sk_names
    assert {"ship-a-feature", "fix-a-bug", "onboard-to-a-new-repo"} <= wf_names
    # No default copies were dropped into the (user-only) global dir.
    assert not (wf._global_dir() / "ship-a-feature.md").exists()
    assert list(wf._global_dir().glob("*.md")) == []
    # idempotent: second call is a no-op migration (nothing left to remove)
    assert wf.ensure_dirs()["workflows"]["removed_seeded"] == 0


def test_migration_removes_stale_seeded_copies(wf, tmp_path):
    # A prior version seeded builtins (marked `source: builtin`) into the global
    # dir; the v2 migration in ensure_dirs removes those stale copies ONCE while
    # leaving user-authored playbooks untouched.
    gdir = wf._global_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    seeded = gdir / "ship-a-feature.md"
    seeded.write_text("---\nname: ship-a-feature\nsource: builtin\n---\n\nold copy\n")
    mine = gdir / "my-flow.md"
    mine.write_text("---\nname: my-flow\ndescription: mine\n---\n\nkeep me\n")

    res = wf.ensure_dirs()
    assert res["workflows"]["removed_seeded"] >= 1
    assert not seeded.exists()             # stale builtin copy removed
    assert mine.exists()                   # user file untouched

    # Migration runs once: re-dropping a seeded copy is NOT re-removed, and the
    # user file still survives.
    seeded.write_text("---\nname: ship-a-feature\nsource: builtin\n---\n\nold copy\n")
    assert wf.ensure_dirs()["workflows"]["removed_seeded"] == 0
    assert seeded.exists() and mine.exists()
