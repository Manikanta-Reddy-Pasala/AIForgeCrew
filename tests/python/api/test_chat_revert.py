"""POST /api/chat/sessions/{id}/revert — undo the files the scope net named:
a file head0 held goes back to it, one it did not hold is deleted (and
untracked when the worktree's seal committed it), nothing outside the chat's
work folder, no symlink, folder or pattern."""
from __future__ import annotations

import importlib
import os
import subprocess

import pytest
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


def _git(cwd, *args):
    subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True)


def _sha(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    (r / "src").mkdir()
    (r / "src" / "m.py").write_text("m\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r, _sha(r)


def _session(r):
    from aiforge_core.runtime import chat_store
    return chat_store.create_session("hello", role="chat", cwd=str(r))["id"]


def _tracked(cwd, p):
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", p], cwd=cwd,
                          capture_output=True).returncode == 0


def _post(client, sid, paths, head0=None):
    """Revert ``paths`` after the scope net offered them for ``head0`` (a
    persisted ``scope_net`` step, what the turn leaves behind)."""
    from aiforge_core.runtime import chat_store
    chat_store.add_message(sid, "assistant", "done",
                           [{"type": "scope_net", "paths": list(paths), "head0": head0}])
    body = {"paths": list(paths)}
    if head0 is not None:
        body["head0"] = head0
    return client.post(f"/api/chat/sessions/{sid}/revert", json=body)


@pytest.fixture
def app(monkeypatch, tmp_path):
    return _fresh_app(monkeypatch, tmp_path)


def test_revert_restores_tracked_and_deletes_untracked(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    (r / "new.py").write_text("y\n")
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["a.py", "new.py"], head0)
    assert res.status_code == 200 and res.json()["reverted"] == ["a.py", "new.py"]
    assert res.json()["ok"] is True
    assert (r / "a.py").read_text() == "x = 1\n" and not (r / "new.py").exists()


def test_revert_refuses_paths_outside_the_work_folder(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg" / "x").write_text("keep\n")
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["../cfg/x", "/etc/hosts", ""], head0)
    assert res.status_code == 200 and res.json()["refused"] == ["../cfg/x", "/etc/hosts", ""]
    assert res.json()["ok"] is False and res.json()["reverted"] == []
    assert (tmp_path / "cfg" / "x").read_text() == "keep\n"


def test_revert_refuses_a_symlink_that_escapes(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (tmp_path / "outside.txt").write_text("keep\n")
    os.symlink(str(tmp_path / "outside.txt"), str(r / "link.txt"))
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["link.txt"], head0)
    assert res.status_code == 200 and res.json()["refused"] == ["link.txt"]
    assert (tmp_path / "outside.txt").exists() and os.path.islink(r / "link.txt")


def test_revert_refuses_a_symlink_inside_the_root(app, tmp_path):
    r, head0 = _repo(tmp_path)
    os.symlink("a.py", str(r / "alias.py"))
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["alias.py"], head0)
    assert res.json()["refused"] == ["alias.py"] and os.path.islink(r / "alias.py")


def test_revert_refuses_folders_and_patterns(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (r / "src" / "m.py").write_text("changed\n")
    (r / "a.py").write_text("changed\n")
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["src", "*", "*.py", "src/*"], head0)
    assert res.json()["refused"] == ["src", "*", "*.py", "src/*"] and res.json()["reverted"] == []
    assert (r / "src" / "m.py").read_text() == "changed\n" and (r / "a.py").read_text() == "changed\n"


def test_revert_refuses_a_path_that_is_not_its_own_normal_form(app, tmp_path):
    """"src/../a.py" stays inside the root but names nothing in a git tree:
    taken as a file to delete, it would remove a.py that head0 holds."""
    r, head0 = _repo(tmp_path)
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["src/../a.py", "./a.py", "src//m.py", "src/"], head0)
    assert res.json()["refused"] == ["src/../a.py", "./a.py", "src//m.py", "src/"]
    assert (r / "a.py").read_text() == "x = 1\n" and _tracked(r, "a.py")


def test_revert_restores_a_file_the_turn_deleted(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (r / "a.py").unlink()
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["a.py"], head0)
    assert res.json()["reverted"] == ["a.py"] and (r / "a.py").read_text() == "x = 1\n"


def test_revert_falls_back_to_head_without_a_worktree(app, tmp_path):
    r, _ = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, ["a.py"], "--not-a-sha")
    assert res.status_code == 200 and res.json()["reverted"] == ["a.py"]
    assert (r / "a.py").read_text() == "x = 1\n"


def _worktree_session(tmp_path, r):
    """A chat whose work folder is a linked worktree of ``r`` (what the chat
    gets in a repository), the way chat_worktree records it."""
    from aiforge_core.runtime import chat_store
    wt = tmp_path / "wt"
    _git(r, "worktree", "add", "-q", "-b", "chat-1", str(wt))
    sid = chat_store.create_session("hello", role="chat", cwd=str(r))["id"]
    chat_store.set_session_workdir(sid, str(wt))
    return sid, wt


def test_sealed_worktree_created_file_is_deleted_and_untracked(app, tmp_path):
    r, _ = _repo(tmp_path)
    sid, wt = _worktree_session(tmp_path, r)
    head0 = _sha(wt)
    (wt / "new.py").write_text("y\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "aiforge chat 1: seal")      # what seal_for_session does at the end of the turn
    assert _tracked(wt, "new.py")
    client = TestClient(app.app)
    res = _post(client, sid, ["new.py"], head0)
    assert res.status_code == 200 and res.json()["reverted"] == ["new.py"]
    assert not (wt / "new.py").exists() and not _tracked(wt, "new.py")


def test_sealed_worktree_modification_goes_back_to_head0(app, tmp_path):
    r, _ = _repo(tmp_path)
    sid, wt = _worktree_session(tmp_path, r)
    head0 = _sha(wt)
    (wt / "a.py").write_text("x = 2\n")
    _git(wt, "commit", "-q", "-am", "aiforge chat 1: seal")
    client = TestClient(app.app)
    res = _post(client, sid, ["a.py"], head0)
    assert res.json()["reverted"] == ["a.py"] and (wt / "a.py").read_text() == "x = 1\n"


def test_sealed_worktree_refuses_a_missing_or_unknown_head0(app, tmp_path):
    r, _ = _repo(tmp_path)
    sid, wt = _worktree_session(tmp_path, r)
    (wt / "a.py").write_text("x = 2\n")
    _git(wt, "commit", "-q", "-am", "aiforge chat 1: seal")
    client = TestClient(app.app)
    assert _post(client, sid, ["a.py"]).status_code == 400
    assert _post(client, sid, ["a.py"], "0" * 40).status_code == 400
    assert (wt / "a.py").read_text() == "x = 2\n"


def test_revert_404_and_409(app, tmp_path, monkeypatch):
    r, _ = _repo(tmp_path)
    client = TestClient(app.app)
    assert client.post("/api/chat/sessions/9999/revert", json={"paths": ["a.py"]}).status_code == 404
    sid = _session(r)
    waited = []
    monkeypatch.setattr("aiforge_core.runtime.chat_runs.settle", lambda _sid, *a, **k: waited.append(_sid) or False)
    assert _post(client, sid, ["a.py"]).status_code == 409
    assert waited == [sid]


def test_revert_restores_a_tracked_file_of_a_subfolder_work_folder(app, tmp_path):
    """Paths are relative to the work folder; git's "<rev>:<path>" reads from
    the repository top. A subfolder chat must get its edited file back, not
    have it deleted as if it were new."""
    r, head0 = _repo(tmp_path)
    (r / "src" / "m.py").write_text("changed\n")
    from aiforge_core.runtime import chat_store
    sid = chat_store.create_session("hello", role="chat", cwd=str(r / "src"))["id"]
    client = TestClient(app.app)
    res = _post(client, sid, ["m.py"], head0)
    assert res.json()["reverted"] == ["m.py"]
    assert (r / "src" / "m.py").read_text() == "m\n" and _tracked(r, "src/m.py")


def test_revert_refuses_a_folder_outside_git(app, tmp_path):
    d = tmp_path / "plain"; d.mkdir()
    (d / "keep.txt").write_text("keep\n")
    from aiforge_core.runtime import chat_store
    sid = chat_store.create_session("hello", role="chat", cwd=str(d))["id"]
    client = TestClient(app.app)
    assert _post(client, sid, ["keep.txt"]).status_code == 400
    assert (d / "keep.txt").read_text() == "keep\n"


def test_revert_refuses_paths_the_scope_net_did_not_offer(app, tmp_path):
    r, head0 = _repo(tmp_path)
    (r / ".env").write_text("SECRET=1\n")
    (r / "new.py").write_text("y\n")
    sid = _session(r)
    client = TestClient(app.app)
    from aiforge_core.runtime import chat_store
    chat_store.add_message(sid, "assistant", "done",
                           [{"type": "scope_net", "paths": ["new.py"], "head0": head0}])
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": [".env"], "head0": head0})
    assert res.json()["refused"] == [".env"] and (r / ".env").exists()
    # offered for another turn (another head0) does not count either
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["new.py"], "head0": "f" * 40})
    assert res.json()["refused"] == ["new.py"] and (r / "new.py").exists()


def test_revert_never_touches_git_internals(app, tmp_path):
    r, head0 = _repo(tmp_path)
    sid = _session(r)
    client = TestClient(app.app)
    res = _post(client, sid, [".git/HEAD", ".git/config"], head0)
    assert res.json()["refused"] == [".git/HEAD", ".git/config"]
    assert (r / ".git" / "HEAD").exists()
