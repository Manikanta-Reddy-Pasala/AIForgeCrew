"""Controlling a run that is already going, and the bookkeeping around a turn.

These endpoints exist for when something is wrong: a run is wedged, a tab was
navigated away from mid-turn, an approval gate is blocking, the agent is going
the wrong way and the user wants to correct it without losing the run. Each
carries a lesson.

  * Stop must not remove the cancel token — popping it microseconds after
    setting it let the slow producer miss the signal and keep executing.
  * Steer must not lie: best-of-N drains no steer queue, so a queued:true
    there would show the user an accepted steer that is dropped at end of turn.
  * Re-attach announces up front whether a run is live, so the client never has
    to guess from the event stream.
  * Restoring a workspace is destructive, so edit-and-resend rolls back ONLY a
    session's own scratch dir — never a shared context folder or a real repo
    someone else has uncommitted work in.

Accepting a predicted next step deliberately executes nothing here; it goes
back through the normal chat message path so it hits the same approval gates.
"""
from __future__ import annotations

import os
import types as pytypes

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import chat as C


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(C.router)
    return TestClient(app)


@pytest.fixture
def session(monkeypatch, tmp_path):
    """One session, id 7, working in tmp_path."""
    from aiforge_core.runtime import chat_store
    state: dict = {"session": {"id": 7, "title": "New chat",
                               "cwd": str(tmp_path), "role": "chat"},
                   "messages": [], "renamed": None}
    monkeypatch.setattr(chat_store, "get_session",
                        lambda sid: state["session"] if sid == 7 else None)
    monkeypatch.setattr(chat_store, "rename_session",
                        lambda sid, t: state.update(renamed=t))
    monkeypatch.setattr(chat_store, "add_message",
                        lambda sid, role, content, meta=None:
                        state["messages"].append((role, content, meta)))
    return state


# ─── re-attaching to a live run ────────────────────────────────────────


def _sse(resp) -> list[dict]:
    import json
    return [json.loads(ln[6:]) for ln in resp.text.splitlines()
            if ln.startswith("data: ")]


@pytest.fixture
def runs(monkeypatch):
    from aiforge_core.runtime import chat_runs
    state: dict = {"run": None, "tail": [{"type": "message", "text": "hi"}]}
    monkeypatch.setattr(chat_runs, "get", lambda sid: state["run"])
    monkeypatch.setattr(chat_runs, "iter_subscription",
                        lambda run, q: iter(state["tail"]))
    return state


def test_a_client_that_comes_back_is_told_a_run_is_live(client, runs):
    runs["run"] = pytypes.SimpleNamespace(done=False, started_at=1000.0,
                                          subscribe=lambda: object())
    evs = _sse(client.get("/api/chat/sessions/7/attach"))
    assert evs[0] == {"type": "attached", "running": True,
                      "started_at": 1000.0}, "elapsed is computed from this"
    assert evs[1] == {"type": "message", "text": "hi"}


def test_nothing_live_ends_the_stream_at_once(client, runs):
    """So the client just shows the persisted history instead of waiting."""
    assert _sse(client.get("/api/chat/sessions/7/attach")) == [
        {"type": "attached", "running": False}, {"type": "done"}]


def test_a_finished_run_is_not_live(client, runs):
    runs["run"] = pytypes.SimpleNamespace(done=True, started_at=1.0,
                                          subscribe=lambda: object())
    assert _sse(client.get("/api/chat/sessions/7/attach"))[0]["running"] is False


# ─── stop + kill-all ───────────────────────────────────────────────────


def test_stopping_a_run_also_unblocks_a_pending_approval(client, monkeypatch):
    from aiforge_core.runtime import chat_approve, chat_cancel
    seen: dict = {}
    monkeypatch.setattr(chat_cancel, "cancel", lambda sid: True)
    monkeypatch.setattr(chat_approve, "cancel", lambda sid: seen.update(sid=sid))
    assert client.post("/api/chat/sessions/7/stop").json() == {
        "stopped": True, "session_id": 7}
    assert seen == {"sid": 7}


def test_stopping_an_idle_session_is_not_an_error(client, monkeypatch):
    from aiforge_core.runtime import chat_approve, chat_cancel
    monkeypatch.setattr(chat_cancel, "cancel", lambda sid: False)
    monkeypatch.setattr(chat_approve, "cancel", lambda sid: None)
    assert client.post("/api/chat/sessions/7/stop").json()["stopped"] is False


def test_kill_all_clears_every_gate_and_frees_the_team_lock(client, monkeypatch):
    """The escape hatch for a wedged run that leaves a new chat waiting on a
    team lock nobody will release."""
    from aiforge_core.runtime import (
        chat_approve,
        chat_cancel,
        chat_interject,
        chat_pipeline,
        chat_runs,
    )
    seen: dict = {"approve": [], "finish": [], "clear": [], "cancel_finish": []}
    monkeypatch.setattr(chat_cancel, "cancel_all", lambda: [1, 2])
    monkeypatch.setattr(chat_cancel, "finish",
                        lambda sid: seen["cancel_finish"].append(sid))
    monkeypatch.setattr(chat_approve, "cancel",
                        lambda sid: seen["approve"].append(sid))
    monkeypatch.setattr(chat_approve, "finish",
                        lambda sid: seen["finish"].append(sid))
    monkeypatch.setattr(chat_interject, "clear",
                        lambda sid: seen["clear"].append(sid))
    monkeypatch.setattr(chat_runs, "finish_all", lambda: None)
    monkeypatch.setattr(chat_pipeline, "force_release_run_lock", lambda: True)
    assert client.post("/api/chat/kill-all").json() == {
        "killed": [1, 2], "count": 2, "team_lock_released": True}
    assert seen["approve"] == [1, 2]
    assert seen["clear"] == [1, 2]
    assert seen["cancel_finish"] == [], \
        "popping the cancel token here lets the slow producer miss it"


# ─── steering ──────────────────────────────────────────────────────────


@pytest.fixture
def interject(monkeypatch):
    from aiforge_core.runtime import chat_interject
    state: dict = {"queued": True, "seen": {}}

    def _push(sid, content, require_steerable=False):
        state["seen"].update(sid=sid, content=content,
                             require=require_steerable)
        return state["queued"]
    monkeypatch.setattr(chat_interject, "push", _push)
    return state


def test_a_steer_is_queued_for_the_live_run(client, interject):
    assert client.post("/api/chat/sessions/7/steer",
                       json={"content": "use postgres"}).json() == {
        "queued": True, "session_id": 7}
    assert interject["seen"]["require"] is True, \
        "test-and-set under one lock — no window for a stale steer"


def test_a_run_that_drains_no_steers_says_so(client, interject):
    """best-of-N never reads the queue; a false queued:true would show the
    user an accepted steer that is silently dropped."""
    interject["queued"] = False
    body = client.post("/api/chat/sessions/7/steer",
                       json={"content": "use postgres"}).json()
    assert body["queued"] is False
    assert body["unsupported"] is True


def test_a_blank_steer_is_refused_as_empty_not_unsupported(client, interject):
    interject["queued"] = False
    body = client.post("/api/chat/sessions/7/steer", json={"content": "  "}).json()
    assert body == {"queued": False, "session_id": 7, "reason": "empty content"}


# ─── approvals ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("decision,resolved", [("approve", True),
                                               ("reject", True),
                                               ("approve", False)])
def test_an_approval_gate_is_resolved(client, monkeypatch, decision, resolved):
    from aiforge_core.runtime import chat_approve
    seen: dict = {}

    def _resolve(sid, dec, note, aid):
        seen.update(sid=sid, dec=dec, note=note, aid=aid)
        return resolved
    monkeypatch.setattr(chat_approve, "resolve", _resolve)
    body = client.post("/api/chat/sessions/7/approve",
                       json={"decision": decision, "id": 4,
                             "note": "go on"}).json()
    assert body == {"resolved": resolved, "decision": decision,
                    "session_id": 7}
    assert seen == {"sid": 7, "dec": decision, "note": "go on", "aid": 4}


def test_an_approval_without_a_note_or_id_still_resolves(client, monkeypatch):
    from aiforge_core.runtime import chat_approve
    seen: dict = {}
    monkeypatch.setattr(chat_approve, "resolve",
                        lambda sid, d, n, i: seen.update(note=n, aid=i) or True)
    client.post("/api/chat/sessions/7/approve", json={"decision": "approve"})
    assert seen == {"note": "", "aid": None}


# ─── checkpoints ───────────────────────────────────────────────────────


@pytest.fixture
def ckpt(monkeypatch):
    from aiforge_core.runtime import checkpoints
    state: dict = {"list": [{"sha": "abc", "label": "before: fix"}], "seen": {}}
    monkeypatch.setattr(checkpoints, "list_checkpoints",
                        lambda cwd: state["list"])
    monkeypatch.setattr(checkpoints, "snapshot",
                        lambda cwd, label=None, when=None:
                        state["seen"].update(label=label, when=when)
                        or {"ok": True, "sha": "def"})
    monkeypatch.setattr(checkpoints, "restore",
                        lambda cwd, sha, paths=None, delete_orphans=False:
                        state["seen"].update(sha=sha, paths=paths,
                                             orphans=delete_orphans)
                        or {"ok": True})
    return state


def test_the_sessions_checkpoints_are_listed(client, session, ckpt):
    assert client.get("/api/chat/sessions/7/checkpoints").json() == {
        "checkpoints": ckpt["list"]}


def test_a_snapshot_is_labelled_and_timestamped(client, session, ckpt):
    r = client.post("/api/chat/sessions/7/checkpoints", json={"label": "wip"})
    assert r.status_code == 201
    assert r.json() == {"ok": True, "sha": "def"}
    assert ckpt["seen"]["label"] == "wip"
    assert ckpt["seen"]["when"]


def test_an_unlabelled_snapshot_is_called_manual(client, session, ckpt):
    client.post("/api/chat/sessions/7/checkpoints", json={})
    assert ckpt["seen"]["label"] == "manual"


def test_a_whole_snapshot_is_restored_by_default(client, session, ckpt):
    r = client.post("/api/chat/sessions/7/checkpoints/restore",
                    json={"sha": "abc1"})
    assert r.json() == {"ok": True}
    assert ckpt["seen"] == {"sha": "abc1", "paths": None, "orphans": False}


def test_a_subset_of_files_can_be_restored(client, session, ckpt):
    client.post("/api/chat/sessions/7/checkpoints/restore",
                json={"sha": "abc1", "paths": ["a.py"], "delete_orphans": True})
    assert ckpt["seen"]["paths"] == ["a.py"]
    assert ckpt["seen"]["orphans"] is True


def test_a_stub_of_a_sha_is_rejected_before_it_reaches_git(client, session,
                                                           ckpt):
    assert client.post("/api/chat/sessions/7/checkpoints/restore",
                       json={"sha": "ab"}).status_code == 422


@pytest.mark.parametrize("method,path,payload", [
    ("get", "/api/chat/sessions/9/checkpoints", None),
    ("post", "/api/chat/sessions/9/checkpoints", {}),
    ("post", "/api/chat/sessions/9/checkpoints/restore", {"sha": "abcd"}),
    ("post", "/api/chat/sessions/9/ticket", {"content": "do it"}),
])
def test_a_missing_session_is_a_404(client, session, ckpt, method, path,
                                    payload):
    call = getattr(client, method)
    r = call(path) if payload is None else call(path, json=payload)
    assert r.status_code == 404


# ─── promoting a chat message to a pipeline ticket ─────────────────────


@pytest.fixture
def tickets(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        C.tickets_mod, "create",
        lambda **kw: seen.update(kw) or pytypes.SimpleNamespace(
            id=41, identifier="ONE-41"))
    return seen


def test_a_chat_message_becomes_an_urgent_interactive_ticket(client, session,
                                                             tickets):
    r = client.post("/api/chat/sessions/7/ticket",
                    json={"content": "add a lexer\nwith tests"})
    assert r.status_code == 201
    assert r.json()["ticket"] == "ONE-41"
    assert r.json()["ticket_id"] == 41
    assert tickets["priority"] == "urgent"
    assert tickets["route"] == "code"
    assert tickets["metadata"] == {"source": "chat", "chat_session_id": 7,
                                   "interactive": True}
    assert tickets["title"] == "add a lexer", "the first line titles it"


def test_the_project_defaults_to_the_sessions_working_dir(client, session,
                                                          tickets, tmp_path):
    client.post("/api/chat/sessions/7/ticket", json={"content": "go"})
    assert tickets["project"] == os.path.basename(str(tmp_path))


def test_an_explicit_project_wins(client, session, tickets):
    client.post("/api/chat/sessions/7/ticket",
                json={"content": "go", "project": "aiforge"})
    assert tickets["project"] == "aiforge"


def test_a_still_unnamed_chat_takes_the_tickets_title(client, session, tickets):
    client.post("/api/chat/sessions/7/ticket", json={"content": "add a lexer"})
    assert session["renamed"] == "add a lexer"


def test_a_named_chat_keeps_its_name(client, session, tickets):
    session["session"]["title"] = "Lexer work"
    client.post("/api/chat/sessions/7/ticket", json={"content": "go"})
    assert session["renamed"] is None


def test_the_transcript_records_the_run(client, session, tickets):
    client.post("/api/chat/sessions/7/ticket", json={"content": "go"})
    roles = [m[0] for m in session["messages"]]
    assert roles == ["user", "assistant"]
    assert "ONE-41" in session["messages"][1][1]
    assert session["messages"][1][2][0]["identifier"] == "ONE-41"


def test_an_empty_ticket_is_rejected(client, session, tickets):
    assert client.post("/api/chat/sessions/7/ticket",
                       json={"content": ""}).status_code == 422


# ─── what the user did with a predicted next step ──────────────────────


@pytest.fixture
def predictions(monkeypatch):
    from aiforge_core.runtime import next_step
    state: dict = {"outcomes": [], "rows": []}
    monkeypatch.setattr(next_step, "outcome",
                        lambda pid, accepted, edited="":
                        state["outcomes"].append((pid, accepted, edited)))
    monkeypatch.setattr(next_step, "history",
                        lambda limit: state.update(limit=limit)
                        or state["rows"])
    return state


def test_an_accepted_prediction_is_recorded_without_executing_anything(
        client, predictions):
    """The chip re-sends the action as an ordinary message so it passes the
    same approval gates — a second execution path is the hole to avoid."""
    assert client.post("/api/chat/suggestion/p1",
                       json={"accepted": True, "edited": "run it"}).json() == {
        "ok": True, "accepted": True}
    assert predictions["outcomes"] == [("p1", True, "run it")]


def test_a_dismissal_is_recorded_too(client, predictions):
    """A feature that learns only from its successes drifts."""
    client.post("/api/chat/suggestion/p1", json={"accepted": False})
    assert predictions["outcomes"] == [("p1", False, "")]


def test_a_bodyless_click_counts_as_a_dismissal(client, predictions):
    client.post("/api/chat/suggestion/p1", content=b"not json",
                headers={"content-type": "application/json"})
    assert predictions["outcomes"] == [("p1", False, "")]


def test_the_counters_answer_whether_the_feature_earns_its_place(client,
                                                                 predictions):
    predictions["rows"] = [{"accepted": True}, {"accepted": False},
                           {"accepted": None}]
    body = client.get("/api/chat/suggestions").json()
    assert body["accepted"] == 1
    assert body["dismissed"] == 1
    assert len(body["suggestions"]) == 3


@pytest.mark.parametrize("asked,used", [(None, 20), (5, 5), (0, 20),
                                        (9999, 200)])
def test_the_history_window_is_bounded(client, predictions, asked, used):
    url = "/api/chat/suggestions" + (f"?limit={asked}" if asked is not None
                                     else "")
    client.get(url)
    assert predictions["limit"] == used


# ─── edit-and-resend ───────────────────────────────────────────────────


@pytest.fixture
def resend(monkeypatch):
    from aiforge_core.runtime import chat_store, checkpoints
    state: dict = {"sha": "abc", "restored": [], "truncated": []}
    monkeypatch.setattr(chat_store, "message_checkpoint",
                        lambda sid, mid: state["sha"])
    monkeypatch.setattr(chat_store, "delete_messages_from",
                        lambda sid, mid: state["truncated"].append(mid))
    monkeypatch.setattr(checkpoints, "restore",
                        lambda cwd, sha: state["restored"].append((cwd, sha)))
    return state


def _body(mid=5):
    return pytypes.SimpleNamespace(edit_from_message_id=mid)


def test_a_resend_in_the_sessions_own_scratch_rolls_the_files_back(resend,
                                                                   tmp_path):
    cwd = tmp_path / "session-7"
    cwd.mkdir()
    C._apply_edit_resend({"cwd": str(cwd)}, 7, _body())
    assert resend["restored"] == [(str(cwd), "abc")]
    assert resend["truncated"] == [5]


def test_a_shared_context_dir_is_never_clobbered(resend, monkeypatch, tmp_path):
    """Another session or the operator has uncommitted work there, and the
    checkpoint sha may not even exist in a rebound repo."""
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path", lambda p: ("jira", "ONE-7"))
    C._apply_edit_resend({"cwd": str(tmp_path)}, 7, _body())
    assert resend["restored"] == []
    assert resend["truncated"] == [5], "history is truncated either way"


def test_a_real_repo_is_never_clobbered(resend, tmp_path):
    C._apply_edit_resend({"cwd": str(tmp_path)}, 7, _body())
    assert resend["restored"] == []


def test_a_turn_with_no_checkpoint_only_truncates(resend, tmp_path):
    resend["sha"] = None
    cwd = tmp_path / "session-7"
    cwd.mkdir()
    C._apply_edit_resend({"cwd": str(cwd)}, 7, _body())
    assert resend["restored"] == []
    assert resend["truncated"] == [5]


def test_an_ordinary_turn_is_not_a_resend(resend):
    C._apply_edit_resend({"cwd": "/x"}, 7, _body(mid=None))
    assert resend["truncated"] == []


def test_a_broken_resend_fails_open(resend, monkeypatch, caplog, tmp_path):
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "message_checkpoint",
                        lambda sid, mid: (_ for _ in ()).throw(OSError("db")))
    C._apply_edit_resend({"cwd": str(tmp_path)}, 7, _body())  # no raise
    assert resend["truncated"] == []


# ─── slash commands ────────────────────────────────────────────────────


@pytest.fixture
def commands(monkeypatch):
    from aiforge_core.runtime import commands as cmds
    state: dict = {"expanded": "Review the diff carefully.",
                   "known": {"review": {}}, "builtin": False}
    monkeypatch.setattr(cmds, "expand",
                        lambda text, cwd: state["expanded"])
    monkeypatch.setattr(cmds, "load", lambda cwd: state["known"])
    monkeypatch.setattr(cmds, "is_builtin", lambda name: state["builtin"])
    return state


def test_a_local_command_is_replaced_by_its_template(commands, tmp_path):
    body = pytypes.SimpleNamespace(content="/review the auth change")
    name, help_text = C._expand_slash_command({"cwd": str(tmp_path)}, body)
    assert name == "review"
    assert help_text is None
    assert body.content == "Review the diff carefully."


def test_help_is_answered_inline_not_sent_to_an_agent(commands, tmp_path):
    commands["known"] = {}
    commands["builtin"] = True
    body = pytypes.SimpleNamespace(content="/help")
    name, help_text = C._expand_slash_command({"cwd": str(tmp_path)}, body)
    assert name is None
    assert help_text == "Review the diff carefully."
    assert body.content == "/help", "the raw text is left alone"


def test_ordinary_text_is_untouched(commands, tmp_path):
    commands["expanded"] = None
    body = pytypes.SimpleNamespace(content="fix the bug")
    assert C._expand_slash_command({"cwd": str(tmp_path)}, body) == (None, None)
    assert body.content == "fix the bug"


def test_a_broken_expansion_leaves_the_raw_text(commands, monkeypatch):
    from aiforge_core.runtime import commands as cmds
    monkeypatch.setattr(cmds, "expand",
                        lambda t, c: (_ for _ in ()).throw(OSError("x")))
    body = pytypes.SimpleNamespace(content="/review x")
    assert C._expand_slash_command({"cwd": "/x"}, body) == (None, None)
    assert body.content == "/review x"


# ─── the resume brief ──────────────────────────────────────────────────


@pytest.fixture
def resume(monkeypatch):
    from aiforge_core.runtime import chat_resume
    state: dict = {"brief": "Rules for this run: finish the parser."}
    monkeypatch.setattr(chat_resume, "resume_preamble",
                        lambda rows, prompt, cwd, forced=None:
                        (_ for _ in ()).throw(state["brief"])
                        if isinstance(state["brief"], Exception)
                        else state["brief"])
    return state


def test_the_brief_folds_into_the_last_user_turn_only(resume):
    """It must never reach `prompt` — the routers, the capture classifier and
    the titler all read that, and 4k of inventory there re-routes the turn."""
    history = [{"role": "user", "content": "first"},
               {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "carry on"}]
    brief = C._apply_resume_brief([], "carry on", "/repo",
                                  pytypes.SimpleNamespace(resume=None), history)
    assert brief == resume["brief"]
    assert history[2]["content"].startswith("carry on\n\n---\n")
    assert history[0]["content"] == "first"


def test_nothing_to_resume_leaves_the_history_alone(resume):
    resume["brief"] = ""
    history = [{"role": "user", "content": "hi"}]
    assert C._apply_resume_brief([], "hi", "/repo",
                                 pytypes.SimpleNamespace(resume=None),
                                 history) == ""
    assert history[0]["content"] == "hi"


def test_a_broken_resume_never_breaks_the_turn(resume):
    resume["brief"] = RuntimeError("x")
    assert C._apply_resume_brief([], "hi", "/repo",
                                 pytypes.SimpleNamespace(resume=None), []) == ""


# ─── binding a scratch session to a durable context ────────────────────


@pytest.fixture
def rehome(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_store, work_context as wc
    root = tmp_path / "chat-workspaces"
    root.mkdir()
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(root))
    state: dict = {"ctx": ("jira", "ONE-7"), "bound": None, "root": root}
    monkeypatch.setattr(wc, "context_for_path", lambda p: None)
    monkeypatch.setattr(wc, "detect_context", lambda prompt: state["ctx"])
    monkeypatch.setattr(wc, "context_dir",
                        lambda kind, key: f"/work/{kind}/{key}")
    monkeypatch.setattr(chat_store, "set_session_cwd",
                        lambda sid, cwd: state.update(bound=(sid, cwd)))
    return state


def test_an_ephemeral_scratch_moves_onto_the_named_contexts_dir(rehome):
    """So that context's scratch survives the session that opened it."""
    cwd = rehome["root"] / "session-7"
    cwd.mkdir()
    assert C._rehome_context_workspace(str(cwd), "look at ONE-7", 7) \
        == "/work/jira/ONE-7"
    assert rehome["bound"] == (7, "/work/jira/ONE-7")


def test_a_real_repo_is_never_hijacked(rehome, tmp_path):
    """Re-homing one would strand the user's work in an empty folder."""
    assert C._rehome_context_workspace(str(tmp_path), "ONE-7", 7) \
        == str(tmp_path)
    assert rehome["bound"] is None


def test_an_already_bound_context_is_left_where_it_is(rehome, monkeypatch):
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path", lambda p: ("jira", "ONE-1"))
    cwd = rehome["root"] / "session-7"
    cwd.mkdir()
    assert C._rehome_context_workspace(str(cwd), "ONE-7", 7) == str(cwd)


def test_a_prompt_naming_no_context_changes_nothing(rehome):
    rehome["ctx"] = None
    cwd = rehome["root"] / "session-7"
    cwd.mkdir()
    assert C._rehome_context_workspace(str(cwd), "hello", 7) == str(cwd)


def test_a_failing_bind_never_blocks_the_turn(rehome, monkeypatch):
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "detect_context",
                        lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    cwd = rehome["root"] / "session-7"
    cwd.mkdir()
    assert C._rehome_context_workspace(str(cwd), "ONE-7", 7) == str(cwd)


# ─── titling ───────────────────────────────────────────────────────────


@pytest.fixture
def titler(monkeypatch):
    from aiforge_core.runtime import chat_store, chat_title
    state: dict = {"prov": "Add A Lexer", "suggested": "Lexer with tests",
                   "renamed": []}
    monkeypatch.setattr(chat_store, "rename_session",
                        lambda sid, t: state["renamed"].append(t))
    monkeypatch.setattr(chat_title, "provisional_title",
                        lambda text: (_ for _ in ()).throw(state["prov"])
                        if isinstance(state["prov"], Exception)
                        else state["prov"])
    monkeypatch.setattr(chat_title, "suggest_title",
                        lambda p, role=None: state.update(role=role)
                        or state["suggested"])
    return state


def test_an_unnamed_chat_is_titled_instantly(titler):
    C._apply_provisional_title(7, pytypes.SimpleNamespace(content="build a lexer"),
                               True)
    assert titler["renamed"] == ["Add A Lexer"]


def test_an_already_named_chat_is_left_alone(titler):
    C._apply_provisional_title(7, pytypes.SimpleNamespace(content="x"), False)
    assert titler["renamed"] == []


def test_a_failed_provisional_falls_back_to_the_raw_message(titler):
    titler["prov"] = RuntimeError("x")
    C._apply_provisional_title(7, pytypes.SimpleNamespace(content="a" * 90),
                               True)
    assert titler["renamed"] == ["a" * 60]


def test_an_empty_provisional_falls_back_too(titler):
    titler["prov"] = ""
    C._apply_provisional_title(7, pytypes.SimpleNamespace(content="hello"), True)
    assert titler["renamed"] == ["hello"]


def test_the_model_title_is_a_cheap_throwaway_call(titler):
    """Routed to triage so it never contends with the turn on a serial local
    endpoint."""
    C._gen_title("build a lexer", 7)
    assert titler["role"] == "triage"
    assert titler["renamed"] == ["Lexer with tests"]


def test_no_suggestion_leaves_the_name_as_it_is(titler):
    titler["suggested"] = ""
    C._gen_title("x", 7)
    assert titler["renamed"] == []


def test_a_titling_failure_never_breaks_the_run(titler, monkeypatch):
    from aiforge_core.runtime import chat_title
    monkeypatch.setattr(chat_title, "suggest_title",
                        lambda p, role=None: (_ for _ in ()).throw(OSError("x")))
    C._gen_title("x", 7)  # no raise


# ─── after the agent finishes ──────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["explain how auth works",
                                    "analyze the parser", "what does X do",
                                    "walk me through the flow"])
def test_a_question_is_read_only(prompt):
    assert C._looks_like_analysis(prompt) is True


@pytest.mark.parametrize("prompt", ["explain how auth works and fix it",
                                    "build a lexer", "add a test"])
def test_an_action_verb_means_it_is_not(prompt):
    assert C._looks_like_analysis(prompt) is False


def test_a_source_write_this_turn_is_read_off_git(tmp_path, monkeypatch):
    """The pre-turn baseline commit means git status shows only THIS turn."""
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: pytypes.SimpleNamespace(
            returncode=0, stdout=" M src/app.py\n?? notes.md\n"))
    assert C._turn_wrote_source(str(tmp_path)) is True


def test_a_qa_turn_touching_no_source_does_not_trigger_a_build(tmp_path,
                                                               monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytypes.SimpleNamespace(
                            returncode=0, stdout=" M notes.md\n\n"))
    assert C._turn_wrote_source(str(tmp_path)) is False


def test_without_git_the_process_wide_touch_list_is_used(tmp_path, monkeypatch):
    import subprocess

    from aiforge_core.runtime import doer_tools
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    monkeypatch.setattr(doer_tools, "touched_paths", lambda: ["/repo/a.py"])
    assert C._turn_wrote_source(str(tmp_path)) is True


def test_a_failing_git_and_no_touch_list_says_no(tmp_path, monkeypatch):
    import subprocess

    from aiforge_core.runtime import doer_tools
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(doer_tools, "touched_paths",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert C._turn_wrote_source(str(tmp_path)) is False


@pytest.fixture
def stack(monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    state: dict = {"detected": {"stacks": ["python"]}, "has_tests": True}
    monkeypatch.setattr(pr, "detect", lambda cwd: state["detected"])
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: state["has_tests"])
    return state


def test_a_repo_with_a_test_stack_is_worth_the_build(stack, tmp_path):
    assert C._worth_verifying(str(tmp_path)) is True


def test_a_doc_only_repo_gets_the_diff_not_a_pointless_build(stack, tmp_path):
    stack["detected"] = {"stacks": []}
    stack["has_tests"] = False
    assert C._worth_verifying(str(tmp_path)) is False


def test_loose_test_files_still_count(stack, tmp_path):
    stack["has_tests"] = False
    (tmp_path / "test_thing.py").write_text("")
    assert C._worth_verifying(str(tmp_path)) is True


def test_an_undetectable_stack_verifies_anyway(stack, monkeypatch, tmp_path):
    from aiforge_core.runtime.tools import project_runner as pr
    monkeypatch.setattr(pr, "detect",
                        lambda cwd: (_ for _ in ()).throw(RuntimeError("x")))
    assert C._worth_verifying(str(tmp_path)) is True


@pytest.fixture
def integration(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    state: dict = {"rep": {"md": "# green", "ok": True}}

    def _reconcile(cwd, ires):
        if isinstance(state["rep"], Exception):
            raise state["rep"]
        ires["rep"] = state["rep"]
        yield {"type": "step", "text": "building"}
    monkeypatch.setattr(ps, "_reconcile_integration", _reconcile)
    return state


def test_a_build_that_actually_ran_reports_back(integration):
    evs = list(C._integration_verify_events("/repo"))
    assert evs[0]["role"] == "verifier"
    assert evs[-1]["text"] == "# green"
    assert evs[-1]["supplementary"] is True, "not persisted as the agent's answer"


def test_a_failing_build_is_reported_too(integration):
    integration["rep"] = {"md": "# red", "ok": False}
    assert list(C._integration_verify_events("/repo"))[-1]["text"] == "# red"


def test_no_toolchain_means_no_report(integration):
    """ok=None — the Changes diff is the useful output there."""
    integration["rep"] = {"md": "# nothing", "ok": None}
    assert [e["type"] for e in C._integration_verify_events("/repo")] == \
        ["thought", "step"]


def test_a_crash_in_the_verifier_never_breaks_the_turn(integration):
    integration["rep"] = RuntimeError("x")
    evs = list(C._integration_verify_events("/repo"))
    assert [e["type"] for e in evs] == ["thought"], "no report, no raise"


@pytest.fixture
def postrun(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    state: dict = {"wrote": True, "worth": True, "changes": [{"type": "changes"}]}
    monkeypatch.setattr(C, "_turn_wrote_source", lambda cwd: state["wrote"])
    monkeypatch.setattr(C, "_worth_verifying", lambda cwd: state["worth"])
    monkeypatch.setattr(C, "_integration_verify_events",
                        lambda cwd: iter([{"type": "verify"}]))
    monkeypatch.setattr(ps, "_emit_changes",
                        lambda cwd, sha, include_worktree=False:
                        iter(state["changes"]))
    return state


def _post(prompt="fix the parser", mode="chat", sha="abc"):
    return [e["type"] for e in C._post_run_events(prompt, "/repo", mode, sha)]


def test_a_turn_that_wrote_code_is_built_and_diffed(postrun):
    assert _post() == ["verify", "changes"]


def test_a_read_only_turn_gets_neither(postrun):
    assert _post("explain how auth works") == []


def test_plan_mode_never_builds(postrun):
    assert _post(mode="plan") == ["changes"]


def test_a_turn_that_wrote_nothing_is_not_built(postrun):
    postrun["wrote"] = False
    assert _post() == ["changes"]


def test_a_doc_edit_still_shows_its_diff(postrun):
    postrun["worth"] = False
    assert _post() == ["changes"]


def test_the_build_can_be_switched_off(postrun, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_INTEGRATION_TEST", "0")
    assert _post() == ["changes"]


def test_a_turn_with_no_baseline_shows_no_diff(postrun):
    assert _post(sha="") == ["verify"]


def test_a_failing_diff_never_breaks_the_turn(postrun, monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    monkeypatch.setattr(ps, "_emit_changes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert _post() == ["verify"]


# ─── the per-turn diff baseline ────────────────────────────────────────


def test_a_reused_workspace_is_committed_before_the_turn(monkeypatch):
    """Otherwise the Changes view shows a previous task's leftovers."""
    import aiforge_core.runtime.parallel_subtasks as ps
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path", lambda p: None)
    monkeypatch.setattr(ps, "_commit_turn_baseline", lambda cwd: "sha1")
    assert C._commit_simple_baseline("/repo") == ("sha1", False)


@pytest.mark.parametrize("kind", ["jira", "confluence", "web"])
def test_a_dossier_folder_gets_no_changes_view(monkeypatch, kind):
    """A plain READ writes ticket.md + attachments there and would otherwise
    report "N files changed"."""
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path", lambda p: (kind, "ONE-7"))
    assert C._commit_simple_baseline("/ctx") == ("", True)


def test_a_repo_context_still_tracks_normally(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path", lambda p: ("repo", "aiforge"))
    monkeypatch.setattr(ps, "_commit_turn_baseline", lambda cwd: "sha2")
    assert C._commit_simple_baseline("/repo") == ("sha2", False)


def test_a_failed_baseline_commit_leaves_no_diff(monkeypatch):
    import aiforge_core.runtime.parallel_subtasks as ps
    from aiforge_core.runtime import work_context as wc
    monkeypatch.setattr(wc, "context_for_path",
                        lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(ps, "_commit_turn_baseline",
                        lambda cwd: (_ for _ in ()).throw(OSError("x")))
    assert C._commit_simple_baseline("/repo") == ("", False)
