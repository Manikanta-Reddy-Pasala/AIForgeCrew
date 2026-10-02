"""Projects page and per-project memory routes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "cfg" / "m.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    monkeypatch.setenv("AIFORGE_PROJECT_INGEST", "0")
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_ON_SWITCH", "0")
    root = tmp_path / "repos"
    (root / "shop").mkdir(parents=True)
    (root / "billing").mkdir()
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(root))
    from aiforge_core.config import repo_map
    monkeypatch.setattr(repo_map, "_load", lambda: {})
    from aiforge_core.memory import projects
    monkeypatch.setattr(projects, "_BOOT_REPO_ROOT", "")
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    from aiforge_core.api.routes import chat as ch
    from aiforge_core.api.routes import projects as pr
    app = FastAPI()
    app.include_router(pr.router)
    app.include_router(ch.router)
    yield TestClient(app), root
    chat_store.reset_backend_for_tests()


def test_lists_folders_with_chat_counts(client):
    c, root = client
    from aiforge_core.runtime import chat_store
    chat_store.create_session("a", str(root / "shop"))
    chat_store.create_session("b", str(root / "shop"))
    chat_store.create_session("c", "/somewhere/else")
    d = c.get("/api/projects").json()
    assert d["root"] == str(root) and d["exists"] is True
    by = {p["name"]: p for p in d["projects"]}
    assert set(by) == {"shop", "billing"}
    assert by["shop"]["chats"] == 2 and by["billing"]["chats"] == 0
    assert by["shop"]["memory_chars"] == 0 and by["shop"]["registered"] is False


def test_missing_root_reports_instead_of_failing(client, monkeypatch, tmp_path):
    c, _ = client
    monkeypatch.setenv("AIFORGE_PROJECTS_ROOT", str(tmp_path / "nope"))
    monkeypatch.setenv("AIFORGE_WORKTREE_ROOT", str(tmp_path / "nope"))
    d = c.get("/api/projects").json()
    assert d["exists"] is False and d["projects"] == []


def test_open_registers_and_unknown_is_404(client):
    c, _ = client
    r = c.post("/api/projects/shop/open")
    assert r.status_code == 200 and r.json()["registered"] is True
    assert c.post("/api/projects/nope/open").status_code == 404
    assert c.post("/api/projects/..%2Fshop/open").status_code == 404


def test_session_list_filters_by_folder_and_create_opens_the_project(client):
    c, root = client
    made = c.post("/api/chat/sessions", json={"cwd": str(root / "shop")}).json()
    c.post("/api/chat/sessions", json={})
    rows = c.get("/api/chat/sessions", params={"cwd": str(root / "shop")}).json()
    assert [r["id"] for r in rows] == [made["id"]]
    assert len(c.get("/api/chat/sessions").json()) == 2
    from aiforge_core.memory import projects
    assert projects.entry("shop") is not None


def test_memory_read_edit_promote_stale_forget(client):
    c, root = client
    from aiforge_core.memory import md_store
    md_store._brief_upsert("shop", "Payments settle nightly")
    md_store._brief_upsert("shop", "Refunds are handled in src/refund.py")
    md_store._brief_upsert("shop", "Always squash commits before merging")

    d = c.get("/api/memory/projects/shop").json()
    assert "Payments settle nightly" in d["text"] and d["cap"] > 0
    assert d["repo_file"].endswith(".aiforge/memory/MEMORY.md")
    assert [p["name"] for p in c.get("/api/memory/projects").json()] == ["shop"]

    r = c.post("/api/memory/projects/shop/promote",
               json={"text": "Always squash commits before merging"})
    assert r.json()["moved"] == 1

    r = c.post("/api/memory/projects/shop/compact", params={"force": False})
    assert r.json()["stale"] == 1
    d = c.get("/api/memory/projects/shop").json()
    assert [s["fact"] for s in d["stale"]] == ["Refunds are handled in src/refund.py"]
    assert "refund.py" not in d["text"] and "squash" not in d["text"]

    r = c.post("/api/memory/projects/shop/stale",
               json={"fact": "Refunds are handled in src/refund.py",
                     "action": "delete"})
    assert r.status_code == 200
    assert c.get("/api/memory/projects/shop").json()["stale"] == []

    r = c.put("/api/memory/projects/shop",
              json={"text": "# shop\n\n## Facts\n\n- Payments settle hourly\n"})
    assert r.status_code == 200
    assert "hourly" in (root / "shop" / ".aiforge" / "memory" / "MEMORY.md").read_text()

    assert c.delete("/api/memory/projects/shop").json()["ok"] is True
    assert c.get("/api/memory/projects").json() == []


def test_learn_switch_round_trips_and_stops_writeback(client, monkeypatch):
    c, root = client
    s = c.post("/api/chat/sessions", json={"cwd": str(root / "shop")}).json()
    assert s["learn"] is True
    off = c.patch(f"/api/chat/sessions/{s['id']}/learn", json={"learn": False})
    assert off.status_code == 200 and off.json()["learn"] is False
    assert c.patch("/api/chat/sessions/9999/learn",
                   json={"learn": False}).status_code == 404

    from aiforge_core.api.routes._chat import _history
    from aiforge_core.runtime import chat_learner, chat_session_fold
    monkeypatch.setattr(chat_learner, "learn_from_chat",
                        lambda **_k: pytest.fail("learned with learning off"))
    _history._chat_learn_writeback(str(root / "shop"), "remember x is y",
                                   "ok", [], s["id"])
    assert chat_session_fold.fold_sync(s["id"])["skipped"]


def test_open_by_path_and_browse(client, monkeypatch, tmp_path):
    c, root = client
    extra = tmp_path / "mnt" / "tools"
    (extra / "lint").mkdir(parents=True)
    monkeypatch.setenv("AIFORGE_SANDBOX", "1")
    monkeypatch.setenv("AIFORGE_MOUNTS", f"{tmp_path / 'cfg'}:{extra}")

    d = c.get("/api/projects").json()
    assert {p["name"] for p in d["projects"]} == {"shop", "billing", "lint"}
    assert [r["path"] for r in d["roots"]] == [str(root), str(extra)]

    hits = c.get("/api/projects/browse", params={"q": str(extra) + "/"}).json()["folders"]
    assert [h["name"] for h in hits] == ["lint"] and hits[0]["openable"] is True

    r = c.post("/api/projects/open", json={"path": str(extra / "lint")})
    assert r.status_code == 200 and r.json()["registered"] is True
    assert r.json()["path"] == str(extra / "lint")

    bad = c.post("/api/projects/open", json={"path": str(tmp_path / "elsewhere")})
    assert bad.status_code == 404 and "mount it first" in bad.json()["detail"]

    # a chat opened there counts for that project
    c.post("/api/chat/sessions", json={"cwd": str(extra / "lint")})
    by = {p["name"]: p for p in c.get("/api/projects").json()["projects"]}
    assert by["lint"]["chats"] == 1 and by["shop"]["chats"] == 0


def test_your_projects_are_the_opened_ones_and_can_be_taken_off(client):
    c, root = client
    by = lambda: {p["name"]: p for p in c.get("/api/projects").json()["projects"]}  # noqa: E731
    assert by()["shop"]["mine"] is False and by()["billing"]["mine"] is False

    c.post("/api/projects/open", json={"path": str(root / "shop")})
    assert by()["shop"]["mine"] is True and by()["billing"]["mine"] is False
    # most recently opened first
    assert c.get("/api/projects").json()["projects"][0]["name"] == "shop"

    # a project that only has chats (opened before this list existed) is yours too
    from aiforge_core.runtime import chat_store
    chat_store.create_session("old", str(root / "billing"))
    assert by()["billing"]["mine"] is True

    assert c.post("/api/projects/remove", json={"path": str(root / "billing")}).json()["ok"]
    assert by()["billing"]["mine"] is False               # chats kept, off the list
    assert by()["billing"]["chats"] == 1
    c.post("/api/projects/open", json={"path": str(root / "billing")})
    assert by()["billing"]["mine"] is True                # opening adds it back
