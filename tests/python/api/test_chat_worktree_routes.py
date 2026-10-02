"""The chat's worktree over HTTP: the info the header shows, and merging."""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    root = tmp_path / "repos"
    repo = root / "shop"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(root))
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "_load", lambda: {})
    from aiforge_core.memory import projects
    monkeypatch.setattr(projects, "_BOOT_REPO_ROOT", "")
    projects.forget_scan()
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    from aiforge_core.api.routes import chat as ch
    app = FastAPI()
    app.include_router(ch.router)

    class E:
        pass

    e = E()
    e.client, e.repo, e.store = TestClient(app), repo, chat_store
    yield e
    chat_store.reset_backend_for_tests()


def test_info_and_merge(env):
    from aiforge_core.runtime import chat_worktree
    sid = env.store.create_session("c", str(env.repo))["id"]
    assert env.client.get(f"/api/chat/sessions/{sid}/worktree").json() == {
        "active": False, "enabled": True}
    wt = chat_worktree.ensure(sid)["path"]
    open(os.path.join(wt, "new.txt"), "w").write("n\n")
    d = env.client.get(f"/api/chat/sessions/{sid}/worktree").json()
    assert d["active"] is True and d["branch"].startswith("aiforge/chat-")
    assert d["base_branch"] == "main" and d["uncommitted"] == 1 and d["ahead"] == 0
    r = env.client.post(f"/api/chat/sessions/{sid}/worktree/merge").json()
    assert r["ok"] is True and r["merged"] == 1
    assert (env.repo / "new.txt").exists()
    d = env.client.get(f"/api/chat/sessions/{sid}/worktree").json()
    assert d["ahead"] == 0 and d["uncommitted"] == 0


def test_a_side_task_asks_about_its_parents_worktree(env):
    from aiforge_core.runtime import chat_worktree
    sid = env.store.create_session("c", str(env.repo))["id"]
    chat_worktree.ensure(sid)
    child = env.store.create_session("side", str(env.repo))["id"]
    env.store.set_session_task(child, sid, {"state": "running"})
    d = env.client.get(f"/api/chat/sessions/{child}/worktree").json()
    assert d["active"] is True and d["branch"].startswith("aiforge/chat-")


def test_merge_is_refused_while_the_chat_is_working(env, monkeypatch):
    from aiforge_core.runtime import chat_runs, chat_worktree
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    sid = env.store.create_session("c", str(env.repo))["id"]
    chat_worktree.ensure(sid)
    chat_runs.start(sid)
    assert env.client.post(f"/api/chat/sessions/{sid}/worktree/merge").status_code == 409
    chat_runs.finish_all()
    assert env.client.get("/api/chat/sessions/9999/worktree").status_code == 404


def test_deleting_the_chat_through_the_api_removes_its_worktree(env):
    from aiforge_core.runtime import chat_worktree
    sid = env.store.create_session("c", str(env.repo))["id"]
    data = chat_worktree.ensure(sid)
    assert env.client.delete(f"/api/chat/sessions/{sid}").status_code == 204
    assert not os.path.exists(data["path"])
    assert data["branch"] in _git(env.repo, "branch", "--list", "aiforge/*").stdout
