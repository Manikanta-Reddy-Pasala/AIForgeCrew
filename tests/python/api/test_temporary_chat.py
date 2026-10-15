"""Temporary chat: nothing of it is learned, captured, traced or kept, and it
is deleted when it is closed."""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_private
    chat_private._KNOWN.clear()
    yield TestClient(api.app)
    # ids restart in every test's fresh store: no flag outlives the test
    chat_private._KNOWN.clear()


def _temp(client):
    return client.post("/api/chat/sessions", json={"temporary": True}).json()


def test_created_temporary_with_learning_off(client):
    s = _temp(client)
    assert s["temporary"] is True and s["learn"] is False and s["title"] == "Temporary chat"
    n = client.post("/api/chat/sessions", json={}).json()
    assert n["temporary"] is False and n["learn"] is True
    from aiforge_core.runtime import chat_private
    assert chat_private.is_temporary(s["id"]) and not chat_private.is_temporary(n["id"])


def test_a_turn_leaves_no_trace_memory_or_transcript(client, tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_persist, chat_store, chat_transcript
    s = _temp(client)
    sid = s["id"]
    wrote = []
    monkeypatch.setattr(chat_persist, "_append_trace", lambda *a, **k: wrote.append("trace"))
    monkeypatch.setattr(chat_persist, "_write_auto_memory", lambda *a, **k: wrote.append("mem"))
    chat_store.add_message(sid, "user", "what is 2+2?")
    chat_persist.persist_turn(session_id=sid, cwd=str(tmp_path), prompt="what is 2+2?",
                              final_text="4", steps=[], team=False,
                              cancelled=False, awaiting=False)
    assert wrote == []
    assert [m["content"] for m in chat_store.get_messages(sid)] == ["what is 2+2?", "4"]
    assert chat_transcript.save(sid, [{"role": "user", "content": "q"}],
                                [{"role": "system", "content": "s"},
                                 {"role": "user", "content": "q"},
                                 {"role": "assistant", "content": "4"}], answer="4") is False


def test_offload_and_log_mirror_write_nothing(client, monkeypatch):
    from aiforge_core.api.routes._chat import _turn_events
    from aiforge_core.runtime import chat_private, context_offload
    s = _temp(client)
    monkeypatch.setattr(chat_private, "current", lambda: True)
    assert context_offload.save("some long text " * 100) is None
    seen = []
    monkeypatch.setattr(_turn_events, "_mirror_event_to_log_",
                        lambda *a, **k: seen.append(1))
    _turn_events._mirror_event_to_log({"type": "message", "text": "hi"}, s["id"], None, None)
    assert seen == []
    from aiforge_core.runtime import perf_recorder
    perf_recorder.record("LLM", "chat", 12.0)
    assert not os.path.exists(perf_recorder._perf_path())


def test_other_chats_never_see_it(client):
    from aiforge_core.runtime import chat_okr_prior, chat_store
    s = _temp(client)
    chat_store.add_message(s["id"], "user", "secret banana plan")
    chat_store.add_message(s["id"], "assistant", "banana plan noted")
    assert chat_store.search_messages("banana plan") == []
    other = client.post("/api/chat/sessions", json={}).json()
    assert chat_okr_prior._most_recent_prior_session(other["id"]) != s["id"]


def test_delete_wipes_its_scratch_folder(client, tmp_path):
    s = _temp(client)
    ws = os.path.join(str(tmp_path / "ws"), f"session-{s['id']}")
    os.makedirs(os.path.join(ws, ".aiforge", "media"), exist_ok=True)
    assert client.delete(f"/api/chat/sessions/{s['id']}").status_code == 204
    assert not os.path.exists(ws)
    assert client.get(f"/api/chat/sessions/{s['id']}").status_code == 404


def test_sweep_deletes_leftovers_but_not_normal_chats(client):
    from aiforge_core.runtime import chat_private
    t = _temp(client)
    n = client.post("/api/chat/sessions", json={}).json()
    assert chat_private.sweep(all_of_them=True) == 1
    ids = {s["id"] for s in client.get("/api/chat/sessions").json()}
    assert t["id"] not in ids and n["id"] in ids


def test_side_task_of_a_temporary_chat_is_temporary(client, monkeypatch):
    from aiforge_core.api.routes._chat import _side_tasks
    from aiforge_core.runtime import chat_store
    # no real turn: a run left going would leak into later tests
    monkeypatch.setattr(_side_tasks, "_start", lambda child, parent: None)
    t = _temp(client)
    child = _side_tasks.create(t["id"], "what does this file do?")
    assert chat_store.get_session(child["id"])["temporary"] is True


def test_okr_compaction_skips_it(client):
    from aiforge_core.runtime import chat_okr
    t = _temp(client)
    assert chat_okr.compact_session(t["id"]).get("skipped") == "disabled"


def test_no_worktree_for_a_temporary_chat(client, tmp_path):
    from aiforge_core.runtime import chat_store, chat_worktree
    t = _temp(client)
    assert chat_worktree.eligible(chat_store.get_session(t["id"])) is None


# ── review fixes ─────────────────────────────────────────────────────────────

def test_learning_cannot_be_switched_back_on(client):
    t = _temp(client)
    r = client.patch(f"/api/chat/sessions/{t['id']}/learn", json={"learn": True})
    if r.status_code == 405:
        r = client.post(f"/api/chat/sessions/{t['id']}/learn", json={"learn": True})
    assert r.status_code == 409


def test_a_deleted_temporary_chat_still_reads_as_temporary(client):
    from aiforge_core.runtime import chat_private, chat_store
    t = _temp(client)
    client.delete(f"/api/chat/sessions/{t['id']}")
    assert chat_private.is_temporary(t["id"])
    assert chat_store.session_learns(t["id"]) is False


def test_reset_forgets_cached_flags(client):
    from aiforge_core.runtime import chat_private
    n = client.post("/api/chat/sessions", json={}).json()
    assert not chat_private.is_temporary(n["id"])
    client.post("/api/chat/sessions/reset")
    t = _temp(client)                                  # ids start at 1 again
    assert chat_private.is_temporary(t["id"])


def test_context_gather_is_blocked_in_a_temporary_chat(client):
    from aiforge_core.runtime.chat_agent._turn._action import _pre_dispatch_gates
    t = _temp(client)

    class St:
        session_id = t["id"]
        convo: list = []
    st = St()
    st.convo = []
    gen = _pre_dispatch_gates(st, "context_gather", {"key": "X-1"}, True, True)
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        sig = stop.value
    assert sig == "continue" and evs[0]["result"]["blocked"] == "temporary_chat"


def test_current_reads_only_this_context(client, monkeypatch):
    from aiforge_core.runtime import chat_cancel, chat_private
    t = _temp(client)
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", str(t["id"]))  # another chat ran last
    chat_cancel.set_active(None)
    assert not chat_private.current()
    chat_cancel.set_active(t["id"])
    assert chat_private.current()
    chat_cancel.set_active(None)


# ── second review ────────────────────────────────────────────────────────────

def _gate(sid, name, args):
    from aiforge_core.runtime.chat_agent._turn._action import _pre_dispatch_gates

    class St:
        session_id = sid
        convo: list = []
    st = St()
    st.convo = []
    gen = _pre_dispatch_gates(st, name, args, True, True)
    evs = []
    try:
        while True:
            evs.append(next(gen))
    except StopIteration as stop:
        return stop.value, evs


def test_reads_that_save_files_are_off_or_stripped(client):
    t = _temp(client)
    sig, evs = _gate(t["id"], "web_crawl", {"url": "https://example.com"})
    assert sig == "continue" and evs[0]["result"]["blocked"] == "temporary_chat"
    args = {"key": "X-1", "images": "true"}
    sig, _ = _gate(t["id"], "jira_read", args)
    assert sig is None and args.get("attachments") == "false" and "images" not in args
    n = client.post("/api/chat/sessions", json={}).json()
    args = {"key": "X-1"}
    _gate(n["id"], "jira_read", args)
    assert "attachments" not in args                    # a normal chat: unchanged


def test_nothing_reaches_langfuse_for_a_temporary_chat(client, monkeypatch):
    from aiforge_core.integrations import langfuse_adapter as lf
    sent = []
    monkeypatch.setattr(lf, "_send", lambda payload: sent.append(payload))
    monkeypatch.setattr(lf.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {
                            "start": lambda self: target(*args)})())
    t = _temp(client)
    n = client.post("/api/chat/sessions", json={}).json()
    lf.record_generation(role="chat", messages=[{"role": "user", "content": "x"}],
                         output="y", session_id=t["id"])
    lf.record_score(name="s", value=1.0, session_id=t["id"])
    assert sent == []
    lf.record_generation(role="chat", messages=[], output="y", session_id=n["id"])
    assert len(sent) == 1


def test_a_store_error_does_not_make_a_known_temporary_chat_normal(client, monkeypatch):
    from aiforge_core.runtime import chat_private, chat_store
    t = _temp(client)

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(chat_store, "get_session", boom)
    assert chat_private.is_temporary(t["id"])


def test_reset_wipes_a_temporary_chats_files_and_keeps_it_known(client, tmp_path):
    from aiforge_core.config.paths import config_dir
    from aiforge_core.runtime import chat_private
    t = _temp(client)
    trace = os.path.join(str(config_dir()), "chat_traces", f"session_{t['id']}.md")
    os.makedirs(os.path.dirname(trace), exist_ok=True)
    open(trace, "w").close()
    client.post("/api/chat/sessions/reset")
    assert not os.path.exists(trace)
    assert chat_private.is_temporary(t["id"])           # a turn still finishing
    n = client.post("/api/chat/sessions", json={}).json()   # takes the id again
    assert n["id"] == t["id"] and not chat_private.is_temporary(n["id"])


def test_a_pipeline_watch_is_off_and_delete_stops_background_work(client, monkeypatch):
    from aiforge_core.runtime import bg_work
    t = _temp(client)
    sig, evs = _gate(t["id"], "gitlab_pipeline_watch", {"project": "p", "pipeline": 1})
    assert sig == "continue" and evs[0]["result"]["blocked"] == "temporary_chat"
    stopped = []
    monkeypatch.setattr(bg_work, "stop_session", lambda sid: stopped.append(sid) or 0)
    n = client.post("/api/chat/sessions", json={}).json()
    client.delete(f"/api/chat/sessions/{n['id']}")
    client.delete(f"/api/chat/sessions/{t['id']}")
    assert stopped == [t["id"]]


def test_a_long_temporary_chat_does_not_crowd_out_search(client):
    from aiforge_core.runtime import chat_store
    n = client.post("/api/chat/sessions", json={}).json()
    chat_store.add_message(n["id"], "user", "the zebra deploy plan")
    t = _temp(client)
    for i in range(520):
        chat_store.add_message(t["id"], "user", f"zebra note {i}")
    hits = chat_store.search_messages("zebra")
    assert hits and {h["session_id"] for h in hits} == {n["id"]}


def test_no_tags_cache_for_a_temporary_chat(client, monkeypatch):
    from aiforge_core.memory import code_context
    from aiforge_core.runtime import chat_private
    monkeypatch.setattr(chat_private, "current", lambda: True)
    assert code_context.aider_digest("/nonexistent", chat_files=[]) == ""
