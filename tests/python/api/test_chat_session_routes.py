"""Chat sessions, attachments, and the per-turn memory writeback.

Two amnesia bugs shape this code. Persisted ``steps`` were never fed back into
context, so any work the model did not transcribe into its final prose simply
vanished from the next turn — hence the ``[did: …]`` digest folded into each
assistant row, and hence keeping an assistant turn that did work but produced
no text (dropping it also broke user/assistant alternation). And deleting a
chat used to discard everything worked out in it, so a delete folds the
session into memory FIRST.

An unpinned chat runs in a scratch workspace, not a repo, so its knowledge is
scoped GLOBAL rather than minting a phantom project per session.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import chat as ch


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ch.router)
    return TestClient(app)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A stubbed chat_store plus a managed workspace root."""
    from aiforge_core.runtime import chat_store
    root = tmp_path / "chat-workspaces"
    root.mkdir()
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(root))
    state: dict = {"session": {"id": 7, "title": "New chat", "cwd": None,
                              "role": "chat"},
                   "sessions": [], "messages": [], "media": [], "root": root,
                   "deleted": True}
    monkeypatch.setattr(chat_store, "create_session",
                        lambda title, cwd, role="chat": dict(state["session"],
                                                             title=title, cwd=cwd,
                                                             role=role))
    monkeypatch.setattr(chat_store, "set_session_cwd",
                        lambda sid, cwd: dict(state["session"], cwd=cwd))
    monkeypatch.setattr(chat_store, "list_sessions", lambda: state["sessions"])
    monkeypatch.setattr(chat_store, "get_session",
                        lambda sid: state["session"] if sid == 7 else None)
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: state["messages"])
    monkeypatch.setattr(chat_store, "rename_session",
                        lambda sid, title: (dict(state["session"], title=title)
                                            if sid == 7 else None))
    monkeypatch.setattr(chat_store, "delete_session", lambda sid: state["deleted"])
    monkeypatch.setattr(chat_store, "delete_all_sessions", lambda: 3)
    monkeypatch.setattr(chat_store, "list_media", lambda sid: state["media"])
    return state


@pytest.fixture
def quiet_background(monkeypatch):
    """Silence the fold / vision-warm side effects."""
    from aiforge_core.runtime import chat_session_fold, vision_detect
    monkeypatch.setattr(chat_session_fold, "fold_previous_async", lambda sid: None)
    monkeypatch.setattr(chat_session_fold, "_enabled", lambda: False)
    monkeypatch.setattr(vision_detect, "warm_vision_async", lambda role: None)


# ─── creating and listing ──────────────────────────────────────────────


def test_an_unpinned_session_gets_its_own_workspace(client, store,
                                                    quiet_background):
    """Isolation: it can build/clean/run without touching other sessions."""
    body = client.post("/api/chat/sessions", json={}).json()
    assert body["cwd"] == os.path.join(str(store["root"]), "session-7")
    assert os.path.isdir(body["cwd"])


def test_a_pinned_cwd_is_left_alone(client, store, quiet_background, tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    body = client.post("/api/chat/sessions", json={"cwd": str(repo)}).json()
    assert body["cwd"] == str(repo)
    assert not (store["root"] / "session-7").exists()


def test_an_unwritable_workspace_does_not_fail_the_create(client, store,
                                                          quiet_background,
                                                          monkeypatch):
    monkeypatch.setattr(os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert client.post("/api/chat/sessions", json={}).status_code == 201


def test_opening_a_new_chat_folds_the_previous_one(client, store, monkeypatch):
    """Moving away from a chat is when its knowledge gets recalled here."""
    from aiforge_core.runtime import chat_session_fold, vision_detect
    folded: list = []
    monkeypatch.setattr(chat_session_fold, "fold_previous_async",
                        lambda sid: folded.append(sid))
    monkeypatch.setattr(vision_detect, "warm_vision_async", lambda role: None)
    client.post("/api/chat/sessions", json={})
    assert folded == [7]


def test_a_failing_fold_never_breaks_session_create(client, store, monkeypatch):
    from aiforge_core.runtime import chat_session_fold, vision_detect
    monkeypatch.setattr(chat_session_fold, "fold_previous_async",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(vision_detect, "warm_vision_async", lambda role: None)
    assert client.post("/api/chat/sessions", json={}).status_code == 201


def test_the_vision_capability_is_identified_before_the_first_upload(
        client, store, monkeypatch):
    from aiforge_core.runtime import chat_session_fold, vision_detect
    monkeypatch.setattr(chat_session_fold, "fold_previous_async", lambda sid: None)
    warmed: list = []
    monkeypatch.setattr(vision_detect, "warm_vision_async",
                        lambda role: warmed.append(role))
    client.post("/api/chat/sessions", json={})
    assert warmed == ["chat"]


def test_sessions_are_listed(client, store):
    store["sessions"] = [{"id": 1}, {"id": 2}]
    assert client.get("/api/chat/sessions").json() == [{"id": 1}, {"id": 2}]


def test_a_session_is_read_with_its_messages(client, store):
    store["messages"] = [{"role": "user", "content": "hi"}]
    body = client.get("/api/chat/sessions/7").json()
    assert body["session"]["id"] == 7
    assert len(body["messages"]) == 1


def test_a_missing_session_is_a_404(client, store):
    assert client.get("/api/chat/sessions/99").status_code == 404


def test_a_session_is_renamed(client, store):
    assert client.patch("/api/chat/sessions/7",
                        json={"title": "New title"}).json()["title"] == "New title"
    assert client.patch("/api/chat/sessions/99",
                        json={"title": "x"}).status_code == 404


# ─── reset + delete ────────────────────────────────────────────────────


def test_reset_wipes_the_rows_the_markers_and_the_workspaces(client, store,
                                                             monkeypatch):
    """Ids restart at 1 after a reset, so a leftover marker would make the new
    session-1 skip folding — silent knowledge loss."""
    from aiforge_core.runtime import chat_okr
    (store["root"] / "session-1").mkdir()
    (store["root"] / "session-9").mkdir()      # orphan, no row
    store["sessions"] = [{"cwd": str(store["root"] / "session-1")}]
    cleared: list = []
    monkeypatch.setattr(chat_okr, "clear_all_markers", lambda: cleared.append(1))
    body = client.post("/api/chat/sessions/reset").json()
    assert body["deleted"] == 3
    assert body["workspaces_removed"] == 2
    assert cleared == [1]
    assert not (store["root"] / "session-9").exists()


def test_a_pinned_repo_survives_a_reset(client, store, monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_okr
    repo = tmp_path / "my-repo"
    repo.mkdir()
    store["sessions"] = [{"cwd": str(repo)}]
    monkeypatch.setattr(chat_okr, "clear_all_markers", lambda: None)
    assert client.post("/api/chat/sessions/reset").json()["workspaces_removed"] == 0
    assert repo.exists()


def test_a_marker_wipe_failure_does_not_fail_the_reset(client, store, monkeypatch):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr(chat_okr, "clear_all_markers",
                        lambda: (_ for _ in ()).throw(RuntimeError("no file")))
    assert client.post("/api/chat/sessions/reset").status_code == 200


@pytest.fixture
def delete_env(monkeypatch, store):
    from aiforge_core.runtime import (chat_approve, chat_cancel, chat_interject,
                                      chat_okr, chat_runs, chat_session_fold)
    calls: list = []
    monkeypatch.setattr(chat_cancel, "cancel", lambda sid: calls.append("cancel"))
    monkeypatch.setattr(chat_approve, "cancel", lambda sid: calls.append("approve"))
    monkeypatch.setattr(chat_interject, "clear", lambda sid: calls.append("interject"))
    monkeypatch.setattr(chat_runs, "finish", lambda sid: calls.append("runs"))
    monkeypatch.setattr(chat_session_fold, "_enabled", lambda: True)
    monkeypatch.setattr(chat_session_fold, "fold_sync",
                        lambda sid: calls.append("fold"))
    monkeypatch.setattr(chat_okr, "forget_session",
                        lambda sid: calls.append("forget"))
    return calls


def test_deleting_a_chat_stops_its_run_and_folds_it_first(client, store,
                                                          delete_env):
    """Otherwise the producer keeps running against a session that no longer
    exists, and everything worked out in the chat is discarded."""
    store["session"]["cwd"] = str(store["root"] / "session-7")
    os.makedirs(store["session"]["cwd"], exist_ok=True)
    assert client.delete("/api/chat/sessions/7").status_code == 204
    assert delete_env[:4] == ["cancel", "approve", "interject", "runs"]
    assert "fold" in delete_env
    assert "forget" in delete_env
    assert not os.path.exists(store["session"]["cwd"])


def test_deleting_a_missing_session_is_a_404(client, store, delete_env):
    store["deleted"] = False
    assert client.delete("/api/chat/sessions/7").status_code == 404


def test_the_fold_can_be_skipped(client, store, delete_env, monkeypatch):
    from aiforge_core.runtime import chat_session_fold
    monkeypatch.setattr(chat_session_fold, "_enabled", lambda: False)
    client.delete("/api/chat/sessions/7")
    assert "fold" not in delete_env


def test_a_failing_marker_cleanup_does_not_fail_the_delete(client, store,
                                                           delete_env, monkeypatch):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr(chat_okr, "forget_session",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("no file")))
    assert client.delete("/api/chat/sessions/7").status_code == 204


# ─── attachments ───────────────────────────────────────────────────────


@pytest.fixture
def media(monkeypatch, store, tmp_path):
    from aiforge_core.runtime import chat_media, chat_store
    state: dict = {"saved": {"ok": True, "path": str(tmp_path / "a.png"),
                             "filename": "a.png", "mime": "image/png",
                             "kind": "image"},
                   "desc": "a chart", "rows": []}
    monkeypatch.setattr(chat_media, "save_file",
                        lambda sid, name, raw: state["saved"])
    monkeypatch.setattr(chat_media, "describe_upload",
                        lambda path, name, mime, role: state["desc"])
    monkeypatch.setattr(chat_media, "vision_enabled", lambda role: True)

    def _add(sid, filename, path, mime=None, description=""):
        row = {"id": 1, "filename": filename, "path": path, "mime": mime,
               "description": description}
        state["rows"].append(row)
        return dict(row)
    monkeypatch.setattr(chat_store, "add_media", _add)
    monkeypatch.setattr(chat_store, "set_media_description",
                        lambda mid, desc: ({"id": mid, "description": desc}
                                           if mid == 1 else None))
    monkeypatch.setattr(chat_store, "delete_media",
                        lambda mid: ({"id": mid, "path": state["saved"]["path"]}
                                     if mid == 1 else None))
    monkeypatch.setattr(chat_store, "get_media",
                        lambda mid: ({"id": mid, "path": state["saved"]["path"],
                                      "mime": "image/png"} if mid == 1 else None))
    return state


def test_an_upload_is_saved_and_described(client, store, media):
    r = client.post("/api/chat/sessions/7/media",
                    files={"file": ("a.png", b"\x89PNG")})
    assert r.status_code == 201
    body = r.json()
    assert body["description"] == "a chart"
    assert body["auto_described"] is True
    assert body["kind"] == "image"


def test_an_undescribable_upload_is_still_stored(client, store, media, monkeypatch):
    """The description is best-effort — losing it must not lose the file."""
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "describe_upload",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("no vlm")))
    body = client.post("/api/chat/sessions/7/media",
                       files={"file": ("a.png", b"x")}).json()
    assert body["auto_described"] is False
    assert body["description"] == ""


def test_an_invalid_upload_is_a_400(client, store, media):
    media["saved"] = {"ok": False, "error": "file_too_large"}
    r = client.post("/api/chat/sessions/7/media", files={"file": ("a.bin", b"x")})
    assert r.status_code == 400
    assert r.json()["detail"] == "file_too_large"


def test_uploading_to_a_missing_session_is_a_404(client, store, media):
    assert client.post("/api/chat/sessions/99/media",
                       files={"file": ("a.png", b"x")}).status_code == 404


def test_the_media_list_reports_whether_the_model_can_see(client, store, media):
    store["media"] = [{"id": 1, "filename": "a.png"}]
    body = client.get("/api/chat/sessions/7/media").json()
    assert body["media"] == [{"id": 1, "filename": "a.png"}]
    assert body["vision"] is True


def test_a_description_can_be_edited(client, store, media):
    assert client.patch("/api/chat/media/1",
                        json={"description": "mine"}).json()["description"] == "mine"
    assert client.patch("/api/chat/media/99", json={"description": "x"}).status_code \
        == 404


def test_deleting_media_unlinks_the_file(client, store, media, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    assert client.delete("/api/chat/media/1").status_code == 204
    assert not (tmp_path / "a.png").exists()


def test_deleting_missing_media_is_a_404(client, store, media):
    assert client.delete("/api/chat/media/99").status_code == 404


def test_a_file_that_is_already_gone_is_not_an_error(client, store, media):
    assert client.delete("/api/chat/media/1").status_code == 204


def test_the_raw_file_is_served(client, store, media, tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG")
    r = client.get("/api/chat/media/1/raw")
    assert r.status_code == 200
    assert r.content == b"\x89PNG"


def test_a_missing_raw_file_is_a_404(client, store, media):
    assert client.get("/api/chat/media/1/raw").status_code == 404
    assert client.get("/api/chat/media/99/raw").status_code == 404


# ─── the turn digest ───────────────────────────────────────────────────


@pytest.mark.parametrize("result,mark", [
    ({"ok": True}, "✓"),
    ({"ok": False}, "✗"),
    ({"error": "boom"}, "✗"),
    ({}, ""),
    ("not a dict", ""),
])
def test_a_tool_outcome_is_marked(result, mark):
    assert ch._step_mark(result) == mark


@pytest.mark.parametrize("args,named", [
    ({"path": "app/store.py"}, "app/store.py"),
    ({"cmd": "pytest -q"}, "pytest -q"),
    ({"query": "lru"}, "lru"),
    ({"unrelated": "x"}, ""),
    ("not a dict", ""),
])
def test_the_argument_worth_naming(args, named):
    assert ch._step_arg(args) == named


def test_a_long_argument_is_shortened():
    assert len(ch._step_arg({"path": "x" * 200})) == 48


def test_the_digest_names_the_tools_and_their_outcomes():
    digest = ch._step_digest([
        {"type": "tool", "name": "file_write", "args": {"path": "a.py"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_tests", "args": {}, "result": {"ok": False}},
        {"type": "thought", "text": "ignored"},
    ])
    assert digest == "file_write(a.py)✓, run_tests✗"


def test_the_digest_is_capped():
    steps = [{"type": "tool", "name": f"t{i}", "result": {"ok": True}}
             for i in range(30)]
    assert ch._step_digest(steps).endswith("…")


def test_a_turn_with_no_tools_has_no_digest():
    assert ch._step_digest([]) == ""
    assert ch._step_digest("not a list") == ""


def test_an_assistant_turn_carries_what_it_did(monkeypatch):
    """Persisted steps were never fed back, so work the model did not
    transcribe into its prose vanished from the next turn."""
    row = {"content": "Done.", "steps": [{"type": "tool", "name": "file_write",
                                          "result": {"ok": True}}]}
    assert ch._history_row_content(row, "assistant") == "Done.\n[did: file_write✓]"


def test_a_silent_assistant_turn_is_still_represented():
    row = {"content": "", "steps": [{"type": "tool", "name": "grep",
                                     "result": {"ok": True}}]}
    assert ch._history_row_content(row, "assistant") == "[did: grep✓]"


def test_a_user_turn_is_verbatim():
    assert ch._history_row_content({"content": " hi "}, "user") == "hi"


def test_history_merges_consecutive_same_role_turns():
    """Some providers reject two user turns in a row."""
    out = ch._chat_history_for_agent([
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "ok"},
        {"role": "system", "content": "dropped"},
        {"role": "assistant", "content": ""},          # nothing to say
    ])
    assert out == [{"role": "user", "content": "first\n\nsecond"},
                   {"role": "assistant", "content": "ok"}]


# ─── the memory writeback ──────────────────────────────────────────────


def test_a_failed_writeback_warns(caplog):
    """On a daemon thread with no HTTP surface, a "remember X" that fails to
    store is real data loss and the warning is the only signal."""
    import logging
    logging.getLogger("aiforge").propagate = True
    with caplog.at_level("WARNING"):
        ch._warn_if_not_persisted({"ok": False, "error": "db locked"},
                                  "chat_learner", "app")
    assert "did NOT persist" in caplog.text


def test_a_skip_is_not_a_failure(caplog):
    with caplog.at_level("WARNING"):
        ch._warn_if_not_persisted({"ok": False, "skipped": "disabled"}, "x", "app")
    assert "did NOT persist" not in caplog.text


@pytest.mark.parametrize("prompt", ["track this as a topic", "organize by module",
                                    "topic: caching"])
def test_a_topic_suggestion_is_captured(monkeypatch, prompt):
    from aiforge_core.memory import md_store
    seen: dict = {}
    monkeypatch.setattr(md_store, "capture",
                        lambda kind, text, repo=None, source=None:
                        seen.update(kind=kind, repo=repo, source=source))
    ch._capture_chat_cue(prompt, "app", 7, pref_captured=False)
    assert seen["kind"] == "topic_suggestion"
    assert seen["source"] == "chat:7"


def test_a_plain_preference_cue_is_captured_as_a_comment(monkeypatch):
    from aiforge_core.memory import md_store
    seen: dict = {}
    monkeypatch.setattr(md_store, "capture",
                        lambda kind, text, repo=None, source=None:
                        seen.update(kind=kind))
    ch._capture_chat_cue("always squash merges", "app", 7, pref_captured=False)
    assert seen["kind"] == "user_comment"


def test_a_turn_already_owned_by_the_preference_capture_is_not_duplicated(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture",
                        lambda *a, **k: pytest.fail("captured the same turn twice"))
    ch._capture_chat_cue("always squash merges", "app", 7, pref_captured=True)


def test_ordinary_chatter_is_not_captured(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture",
                        lambda *a, **k: pytest.fail("captured plain chatter"))
    ch._capture_chat_cue("what does this do?", "app", 7, pref_captured=False)


def test_a_capture_failure_is_swallowed(monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    ch._capture_chat_cue("topic: x", "app", 7, pref_captured=False)


def test_the_writeback_runs_preference_capture_and_the_learner(monkeypatch):
    from aiforge_core.runtime import chat_learner, preference_capture
    import aiforge_core.runtime.chat_agent as ca
    calls: list = []
    monkeypatch.setattr(ca, "_chat_repo_key", lambda cwd: "app")
    monkeypatch.setattr(preference_capture, "capture",
                        lambda prompt, repo=None, session_id=None:
                        calls.append("pref") or {"captured": True})
    monkeypatch.setattr(chat_learner, "learn_from_chat",
                        lambda **kw: calls.append("learn") or {"ok": True})
    monkeypatch.setattr(ch, "_capture_chat_cue",
                        lambda p, r, s, c: calls.append(f"cue:{c}"))
    ch._chat_learn_writeback("/repo", "always squash", "done", [], 7)
    assert calls == ["pref", "learn", "cue:True"]


def test_a_crash_in_the_writeback_never_affects_the_turn(monkeypatch):
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(ca, "_chat_repo_key",
                        lambda cwd: (_ for _ in ()).throw(RuntimeError("no git")))
    ch._chat_learn_writeback("/repo", "p", "a", [], 7)


# ─── the periodic session summary ──────────────────────────────────────


@pytest.fixture
def summary(monkeypatch):
    from aiforge_core.memory import okf
    from aiforge_core.runtime import (chat_store, chat_summary, session_ledger)
    import aiforge_core.runtime.chat_agent as ca
    state: dict = {"messages": [{"role": "user", "content": "hi"}] * 4,
                   "summarised": [], "okr_repo": "unset"}
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: state["messages"])
    monkeypatch.setattr(ca, "_chat_repo_key", lambda cwd: "app")
    monkeypatch.setattr(chat_summary, "summarize_session",
                        lambda sid, repo: state["summarised"].append(repo))
    monkeypatch.setattr(session_ledger, "capture_working_workflow",
                        lambda sid, repo: None)
    monkeypatch.setattr(session_ledger, "ledger_block", lambda sid: "")
    monkeypatch.setattr(okf, "extract_and_save",
                        lambda tx, repo=None: state.update(okr_repo=repo))
    monkeypatch.delenv("AIFORGE_CHAT_SUMMARY_EVERY", raising=False)
    return state


def test_the_summary_refreshes_on_the_boundary(summary):
    ch._chat_summarize_session("/repo", 7)
    assert summary["summarised"] == ["app"]


def test_a_turn_off_the_boundary_does_nothing(summary):
    summary["messages"] = [{"role": "user"}] * 3
    ch._chat_summarize_session("/repo", 7)
    assert summary["summarised"] == []


def test_the_boundary_is_tunable(summary, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SUMMARY_EVERY", "3")
    summary["messages"] = [{"role": "user"}] * 3
    ch._chat_summarize_session("/repo", 7)
    assert summary["summarised"] == ["app"]


def test_a_junk_boundary_falls_back(summary, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SUMMARY_EVERY", "often")
    ch._chat_summarize_session("/repo", 7)
    assert summary["summarised"] == ["app"]


def test_an_unpinned_scratch_session_scopes_its_knowledge_globally(
        summary, monkeypatch):
    """Otherwise every session mints a phantom projects/session-<id>/ tree."""
    monkeypatch.setattr(ch, "_is_isolated_workspace", lambda cwd: True)
    ch._chat_summarize_session("/ws/session-7", 7)
    assert summary["okr_repo"] is None


def test_a_pinned_repo_session_scopes_to_that_repo(summary, monkeypatch):
    monkeypatch.setattr(ch, "_is_isolated_workspace", lambda cwd: False)
    ch._chat_summarize_session("/repo", 7)
    assert summary["okr_repo"] == "app"


def test_a_crash_in_the_summary_never_affects_the_turn(summary, monkeypatch):
    from aiforge_core.runtime import chat_summary
    monkeypatch.setattr(chat_summary, "summarize_session",
                        lambda sid, repo: (_ for _ in ()).throw(RuntimeError("no llm")))
    ch._chat_summarize_session("/repo", 7)


# ─── per-session reads ─────────────────────────────────────────────────


def test_the_turn_trace_is_served(client, store, monkeypatch):
    from aiforge_core.runtime import chat_trace
    monkeypatch.setattr(chat_trace, "read_turns",
                        lambda sid: [{"ts": 1, "mode": "simple"}])
    body = client.get("/api/chat/sessions/7/trace").json()
    assert body["count"] == 1
    assert body["session_id"] == 7


def test_the_llm_usage_meter_is_served(client, store, monkeypatch):
    from aiforge_core.llm import call_meter
    monkeypatch.setattr(call_meter, "snapshot",
                        lambda sid: {"turn": 3, "session": 12, "per_minute": 2})
    body = client.get("/api/chat/sessions/7/llm-usage").json()
    assert body == {"session_id": 7, "turn": 3, "session": 12, "per_minute": 2}


def test_the_planners_spec_is_served_when_it_exists(client, store, tmp_path):
    store["session"]["cwd"] = str(tmp_path)
    (tmp_path / "SPEC.md").write_text("# SPEC\nthe plan")
    body = client.get("/api/chat/sessions/7/spec").json()
    assert body["exists"] is True
    assert "the plan" in body["content"]


def test_no_spec_yet(client, store, tmp_path):
    store["session"]["cwd"] = str(tmp_path)
    assert client.get("/api/chat/sessions/7/spec").json() == {"exists": False,
                                                              "content": ""}


def test_an_unreadable_spec_reports_the_error(client, store, tmp_path, monkeypatch):
    store["session"]["cwd"] = str(tmp_path)
    (tmp_path / "SPEC.md").write_text("x")
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    body = client.get("/api/chat/sessions/7/spec").json()
    assert body["exists"] is False
    assert "locked" in body["error"]


def test_an_explicit_compaction_distils_the_session(client, store, monkeypatch):
    from aiforge_core.runtime import chat_okr
    import aiforge_core.runtime.chat_agent as ca
    store["session"]["cwd"] = "/repo"
    monkeypatch.setattr(ca, "_chat_repo_key", lambda cwd: "app")
    seen: dict = {}
    monkeypatch.setattr(chat_okr, "compact_session",
                        lambda sid, repo=None: seen.update(sid=sid, repo=repo)
                        or {"ok": True})
    assert client.post("/api/chat/sessions/7/compact").json() == {"ok": True}
    assert seen == {"sid": 7, "repo": "app"}


def test_compacting_a_missing_session_is_a_404(client, store):
    assert client.post("/api/chat/sessions/99/compact").status_code == 404
