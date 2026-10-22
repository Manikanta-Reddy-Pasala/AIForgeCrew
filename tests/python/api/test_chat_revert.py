"""POST /api/chat/sessions/{id}/revert — undo the files the scope net named:
tracked files back to head0, untracked files deleted, nothing outside the
chat's work folder."""
from __future__ import annotations

import importlib
import os
import subprocess

from fastapi.testclient import TestClient

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api


def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], GIT + ["commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=r, check=True)
    head0 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    return r, head0


def _session(r):
    from aiforge_core.runtime import chat_store
    return chat_store.create_session("hello", role="chat", cwd=str(r))["id"]


def test_revert_restores_tracked_and_deletes_untracked(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    r, head0 = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    (r / "new.py").write_text("y\n")
    sid = _session(r)
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["a.py", "new.py"], "head0": head0})
    assert res.status_code == 200 and res.json()["reverted"] == ["a.py", "new.py"]
    assert res.json()["ok"] is True
    assert (r / "a.py").read_text() == "x = 1\n" and not (r / "new.py").exists()


def test_revert_refuses_paths_outside_the_work_folder(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    r, head0 = _repo(tmp_path)
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg" / "x").write_text("keep\n")
    sid = _session(r)
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["../cfg/x", "/etc/hosts", ""], "head0": head0})
    assert res.status_code == 200 and res.json()["refused"] == ["../cfg/x", "/etc/hosts", ""]
    assert res.json()["ok"] is False and res.json()["reverted"] == []
    assert (tmp_path / "cfg" / "x").read_text() == "keep\n"


def test_revert_refuses_a_symlink_that_escapes(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    r, head0 = _repo(tmp_path)
    (tmp_path / "outside.txt").write_text("keep\n")
    os.symlink(str(tmp_path / "outside.txt"), str(r / "link.txt"))
    sid = _session(r)
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["link.txt"], "head0": head0})
    assert res.status_code == 200 and res.json()["refused"] == ["link.txt"]
    assert (tmp_path / "outside.txt").exists() and os.path.islink(r / "link.txt")


def test_revert_falls_back_to_head_when_head0_is_not_a_commit(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    r, _ = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    sid = _session(r)
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["a.py"], "head0": "--not-a-sha"})
    assert res.status_code == 200 and res.json()["reverted"] == ["a.py"]
    assert (r / "a.py").read_text() == "x = 1\n"


def test_revert_404_and_409(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    r, head0 = _repo(tmp_path)
    client = TestClient(api.app)
    assert client.post("/api/chat/sessions/9999/revert", json={"paths": ["a.py"]}).status_code == 404
    sid = _session(r)
    monkeypatch.setattr("aiforge_core.runtime.chat_runs.is_running", lambda _sid: True)
    assert client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["a.py"]}).status_code == 409
