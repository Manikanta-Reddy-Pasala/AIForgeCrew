"""Team/build runs work in the folder the user named, not the session scratch.

Live bug: "In /Users/me/live-ws fix money.py so every test in tests/ passes"
ran the team pipeline in chat-workspaces/session-1, created a literal
``Users/me/live-ws/`` subfolder there, and answered "✅ Built — all tests
pass" without ever running the user's tests.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_target as tt


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return os.path.realpath(str(path))


@pytest.fixture
def session_ws(tmp_path):
    ws = tmp_path / "chat-workspaces" / "session-1"
    ws.mkdir(parents=True)
    return str(ws)


def test_an_absolute_named_folder_is_the_target(tmp_path, session_ws):
    repo = _git_repo(tmp_path / "live-ws")
    t = tt.resolve_team_target(
        [f"In {repo} fix money.py so every test in tests/ passes."], session_ws)
    assert t.retargeted
    assert t.cwd == repo and t.named == repo
    assert not t.missing


def test_a_tilde_path_expands_to_home(tmp_path, session_ws, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _git_repo(tmp_path / "proj")
    t = tt.resolve_team_target(["fix ~/proj/money.py please"], session_ws)
    assert t.cwd == repo


def test_a_folder_inside_a_git_repo_resolves_to_the_repo_root(tmp_path,
                                                              session_ws):
    repo = _git_repo(tmp_path / "mono")
    sub = tmp_path / "mono" / "pkg" / "money"
    sub.mkdir(parents=True)
    t = tt.resolve_team_target([f"fix the code in {sub}"], session_ws)
    assert t.cwd == repo
    assert t.named == os.path.realpath(str(sub))


def test_a_file_to_create_in_an_existing_folder_targets_that_folder(
        tmp_path, session_ws):
    d = tmp_path / "plain"
    d.mkdir()
    t = tt.resolve_team_target([f"create {d}/new_mod.py"], session_ws)
    assert t.cwd == os.path.realpath(str(d))


def test_a_missing_folder_asks_instead_of_inventing_one(tmp_path, session_ws):
    gone = tmp_path / "does-not-exist" / "live-ws"
    t = tt.resolve_team_target([f"In {gone} fix money.py"], session_ws)
    assert not t.retargeted
    assert t.missing == [str(gone)]
    assert t.cwd == session_ws
    assert "can't find" in tt.clarify_text(t.missing)


def test_no_path_keeps_the_session_workspace(session_ws):
    t = tt.resolve_team_target(["build a todo app with tests"], session_ws)
    assert t.cwd == session_ws and not t.retargeted and not t.missing


def test_a_url_route_is_not_a_folder(session_ws):
    t = tt.resolve_team_target(["add a GET /api/users endpoint"], session_ws)
    assert t.cwd == session_ws and not t.missing


def test_a_path_inside_the_workspace_changes_nothing(session_ws):
    t = tt.resolve_team_target([f"edit {session_ws}/app.py"], session_ws)
    assert t.cwd == session_ws and not t.retargeted


def test_a_lone_file_outside_any_repo_is_a_reference_not_a_target(
        tmp_path, session_ws):
    log = tmp_path / "app.log"
    log.write_text("x")
    t = tt.resolve_team_target([f"read {log} and build a parser"], session_ws)
    assert t.cwd == session_ws


def test_a_follow_up_keeps_the_folder_an_earlier_turn_named(tmp_path,
                                                            session_ws):
    repo = _git_repo(tmp_path / "r")
    texts = tt.user_texts("now also handle negative amounts", [
        {"role": "user", "content": f"In {repo} fix money.py"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now also handle negative amounts"},
    ])
    assert tt.resolve_team_target(texts, session_ws).cwd == repo


def test_the_enhancer_restatement_is_not_consent(tmp_path, session_ws):
    repo = _git_repo(tmp_path / "other")
    texts = tt.user_texts("fix it", [{"role": "user", "content":
        f"fix it\n\n---\n[Interpreted request — x]\nwork in {repo}"}])
    assert tt.resolve_team_target(texts, session_ws).cwd == session_ws


# ─── subtask paths never become path-shaped folders ─────────────────────


def test_an_absolute_subtask_path_under_the_target_becomes_relative(tmp_path):
    repo = _git_repo(tmp_path / "live-ws")
    subs, dropped = tt.anchor_subtask_paths(
        [{"slug": "a", "path": f"{repo}/money.py"},
         {"slug": "b", "path": repo.lstrip("/") + "/tests/test_money.py"},
         {"slug": "c", "path": "src/app.py"}], repo)
    assert [s["path"] for s in subs] == ["money.py", "tests/test_money.py",
                                         "src/app.py"]
    assert dropped == []


def test_a_subtask_path_outside_the_workspace_is_dropped(tmp_path, session_ws):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subs, dropped = tt.anchor_subtask_paths(
        [{"slug": "a", "path": f"{elsewhere}/money.py"},
         {"slug": "b", "path": str(elsewhere).lstrip("/") + "/x/money.py"},
         {"slug": "c", "path": "money.py"}], session_ws)
    assert [s["slug"] for s in subs] == ["c"]
    assert len(dropped) == 2


# ─── the chat route hands the named folder to the pipeline ─────────────


def _route_decision(**kw):
    base = {"doc_task": False, "route_pipeline": True, "notice": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _dispatch(monkeypatch, prompt, cwd, team=False, route_pipeline=True):
    from aiforge_core.api.routes._chat import _stages
    seen: dict = {}

    def _fake_pipeline(_pp, prompt, cwd, *a, **k):
        seen["cwd"] = cwd
        a[-1]["done"] = True
        yield {"type": "message", "text": "built"}
    monkeypatch.setattr(_stages, "_pipeline_route", _fake_pipeline)
    rctx = {"done": False}
    evs = list(_stages._dispatch_agent_route(
        _route_decision(route_pipeline=route_pipeline), None, prompt, cwd,
        None, [{"role": "user", "content": prompt}], lambda t: t, {}, 0.0,
        team, "", rctx))
    return seen, evs, rctx


def test_the_pipeline_route_runs_in_the_named_repo(tmp_path, session_ws,
                                                   monkeypatch):
    repo = _git_repo(tmp_path / "live-ws-team-varied")
    seen, evs, rctx = _dispatch(
        monkeypatch, f"In {repo} fix money.py so every test in tests/ passes. "
        "Run pytest there to check. Do not edit the tests.", session_ws)
    assert seen["cwd"] == repo
    assert any(repo in (e.get("text") or "") for e in evs
               if e.get("role") == "router")
    assert not os.path.exists(os.path.join(session_ws, repo.lstrip("/")))


def test_a_missing_named_folder_stops_with_a_question(tmp_path, session_ws,
                                                      monkeypatch):
    seen, evs, rctx = _dispatch(
        monkeypatch, f"In {tmp_path}/nope/live-ws fix money.py", session_ws)
    assert "cwd" not in seen and rctx["done"]
    assert evs[-1]["awaiting_input"] is True


def test_the_sequential_team_route_gets_the_named_repo(tmp_path, session_ws,
                                                       monkeypatch):
    import aiforge_core.runtime.chat_pipeline as cp
    repo = _git_repo(tmp_path / "seq")
    seen = {}

    def _fake_stream(prompt, *, cwd, **kw):
        seen["cwd"] = cwd
        yield {"type": "done"}
        return False
    monkeypatch.setattr(cp, "stream_chat_pipeline", _fake_stream)
    _dispatch(monkeypatch, f"fix {repo}/money.py", session_ws, team=True,
              route_pipeline=False)
    assert seen["cwd"] == repo
