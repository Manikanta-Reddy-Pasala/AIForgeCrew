"""Side tasks: a second agent run beside the one a chat is on.

A chat session holds one run, so an independent request that arrives mid-run
becomes a child chat with its own run. These tests pin the decisions: what
steers and what becomes a task, what starts now and what waits, and that a
finished task reports back into the parent chat without cutting into a turn
that is still running.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "cfg" / "chat.db"))
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_ON_SWITCH", "0")
    monkeypatch.setenv("AIFORGE_CHAT_SIDE_TASKS_MAX", "3")
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    from aiforge_core.api.routes import chat as ch
    from aiforge_core.api.routes._chat import _side_tasks as st

    running: set = set()
    started: list = []
    monkeypatch.setattr(st, "_is_running", lambda sid: sid in running)

    def _fake_start(child, parent):
        st._set_task(child, state=st.RUNNING)
        running.add(child["id"])
        started.append(child["id"])

    monkeypatch.setattr(st, "_start", _fake_start)
    app = FastAPI()
    app.include_router(ch.router)

    class Env:
        pass

    e = Env()
    e.client, e.st, e.store = TestClient(app), st, chat_store
    e.running, e.started = running, started
    e.cwd = str(tmp_path / "proj")

    def _parent(prompt="refactor the billing module in billing.py", mode="simple"):
        s = chat_store.create_session("main", e.cwd)
        chat_store.add_message(s["id"], "user", prompt, mode=mode)
        running.add(s["id"])
        return s["id"]

    def _finish(sid, answer=None):
        if answer is not None:
            chat_store.add_message(sid, "assistant", answer)
        running.discard(sid)
        st.on_run_finished(sid)

    e.parent, e.finish = _parent, _finish
    yield e
    chat_store.reset_backend_for_tests()


def _state(env, child_id):
    return (env.store.get_session(child_id).get("task") or {}).get("state")


@pytest.mark.parametrize("text,want", [
    ("no, use the v2 endpoint instead", "steer"),
    ("stop", "steer"),
    ("also handle the empty list case", "steer"),
    ("make the button blue", "steer"),
    ("run another agent to check the logs for errors", "task"),
    ("meanwhile summarise the README", "task"),
    ("what does the retry helper do?", "task"),
    ("new task: list the open TODOs", "task"),
    ("in parallel, count the lines of python", "task"),
])
def test_steer_or_task(env, text, want):
    assert env.st.classify(text) == want


def test_a_reading_task_starts_beside_the_running_turn(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    assert t["state"] == "running" and t["edits"] is False
    assert env.started == [t["id"]]
    child = env.store.get_session(t["id"])
    assert child["parent_id"] == pid and child["cwd"] == env.cwd


def test_an_editing_task_waits_for_the_editing_turn_then_starts(env):
    pid = env.parent()                                   # parent is editing
    t = env.st.create(pid, "rename the helper function in utils.py")
    assert t["state"] == "queued" and t["edits"] is True and env.started == []
    env.finish(pid, "refactor done")
    assert _state(env, t["id"]) == "running" and env.started == [t["id"]]


def test_an_editing_task_runs_at_once_when_the_turn_only_reads(env):
    pid = env.parent(prompt="explain how billing works")
    t = env.st.create(pid, "rename the helper function in utils.py")
    assert t["state"] == "running"
    # …and a second editing task waits for the first one
    t2 = env.st.create(pid, "fix the typo in the README.md file")
    assert t2["state"] == "queued"
    env.finish(t["id"], "renamed")
    assert _state(env, t2["id"]) == "running"


def test_no_spare_slot_queues_everything(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SIDE_TASKS_MAX", "1")
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    assert t["state"] == "queued"
    env.finish(pid, "done")
    assert _state(env, t["id"]) == "running"


def test_the_limit_counts_the_parent(env):
    pid = env.parent()                                   # limit 3: parent + 2
    ids = [env.st.create(pid, f"what is item {i}?")["id"] for i in range(3)]
    assert [_state(env, i) for i in ids] == ["running", "running", "queued"]


def test_result_is_posted_when_the_parent_is_idle_not_mid_turn(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    env.finish(t["id"], "It retries three times with backoff.")
    assert _state(env, t["id"]) == "done"
    rows = env.store.get_messages(pid)
    assert [m["role"] for m in rows] == ["user"]         # parent still running
    env.finish(pid, "refactor done")
    rows = env.store.get_messages(pid)
    assert [m["role"] for m in rows] == ["user", "assistant", "assistant"]
    assert rows[1]["content"] == "refactor done"         # its own answer first
    assert rows[2]["content"].startswith("**Side task:** what does the retry helper do?")
    assert "retries three times" in rows[2]["content"]
    assert rows[2]["steps"][0] == {"type": "side_task", "session_id": t["id"],
                                   "title": "what does the retry helper do?"}
    # posted once
    env.st.on_run_finished(pid)
    assert len(env.store.get_messages(pid)) == 3


def test_a_stopped_task_posts_nothing(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    env.finish(t["id"])                                   # no answer: stopped
    env.finish(pid, "refactor done")
    assert _state(env, t["id"]) == "stopped"
    assert [m["role"] for m in env.store.get_messages(pid)] == ["user", "assistant"]


def test_route_send_when_idle_steer_and_task_when_busy(env, monkeypatch):
    s = env.store.create_session("main", env.cwd)
    r = env.client.post(f"/api/chat/sessions/{s['id']}/side",
                        json={"content": "what is this?"})
    assert r.json() == {"action": "send"}

    pid = env.parent()
    from aiforge_core.runtime import chat_interject
    chat_interject.set_steerable(pid, True)
    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "also handle the empty list case"})
    assert r.json()["action"] == "steer" and r.json()["queued"] is True
    chat_interject.clear(pid)

    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "what does the retry helper do?"})
    assert r.json()["action"] == "task" and r.json()["task"]["state"] == "running"

    # the user can force either way
    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "also handle the empty list case",
                              "as": "task"})
    assert r.json()["action"] == "task"

    tasks = env.client.get(f"/api/chat/sessions/{pid}/tasks").json()
    assert [t["state"] for t in tasks["tasks"]] == ["running", "running"]
    assert tasks["limit"] == 3 and tasks["running"] is True
    assert env.client.post("/api/chat/sessions/9999/side",
                           json={"content": "x"}).status_code == 404


def test_an_unsteerable_run_gets_a_task_instead_of_a_lost_message(env):
    pid = env.parent()                                   # not marked steerable
    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "also handle the empty list case"})
    assert r.json()["action"] == "task"


def test_listing_catches_up_after_a_restart(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    # the API restarted: no run is alive, the answer is in the store
    env.store.add_message(t["id"], "assistant", "It retries three times.")
    env.running.clear()
    tasks = env.client.get(f"/api/chat/sessions/{pid}/tasks").json()["tasks"]
    assert tasks[0]["state"] == "done" and tasks[0]["posted"] is True
    assert "retries three times" in tasks[0]["preview"]
    assert "retries three times" in env.store.get_messages(pid)[-1]["content"]


def test_deleting_the_chat_deletes_its_side_tasks(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    env.running.clear()
    assert env.client.delete(f"/api/chat/sessions/{pid}").status_code == 204
    assert env.store.get_session(t["id"]) is None


def test_the_side_agent_is_told_it_is_a_side_task(env):
    pid = env.parent()
    env.store.add_message(pid, "assistant", "I am moving the tax code.")
    pre = env.st._context_preamble(env.store.get_session(pid))
    assert "SIDE TASK" in pre and "moving the tax code" in pre
    assert "do not continue that chat's work" in pre


# ── status questions, steer acknowledgements, and answers shown at once ───────

def test_a_status_question_is_answered_from_the_run_not_queued_or_spun_off(env, monkeypatch):
    from aiforge_core.runtime import chat_interject, chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    pid = env.parent()
    run = chat_runs.start(pid)
    run.publish({"type": "tool_start", "name": "run_command",
                 "args": {"cmd": "sleep 70"}, "call_id": 1})
    chat_interject.set_steerable(pid, True)
    for q in ("what's the status?", "status", "how far along are you?"):
        r = env.client.post(f"/api/chat/sessions/{pid}/side", json={"content": q}).json()
        assert r["action"] == "status", q
        assert "**Now:** `run_command` `sleep 70`" in r["text"]
    assert chat_interject.pending(pid) == 0                 # no steer was queued
    assert env.store.child_sessions(pid) == []              # no side agent either
    chat_runs.finish_all()


def test_a_status_question_is_a_task_only_when_the_user_says_so(env, monkeypatch):
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    pid = env.parent()
    chat_runs.start(pid)
    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "what's the status?", "as": "task"}).json()
    assert r["action"] == "task"
    chat_runs.finish_all()


def test_a_steer_says_when_it_will_be_read(env, monkeypatch):
    from aiforge_core.runtime import chat_interject, chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    pid = env.parent()
    run = chat_runs.start(pid)
    run.publish({"type": "tool_start", "name": "command_wait",
                 "args": {"id": "bg-6"}, "call_id": 1})
    chat_interject.set_steerable(pid, True)
    r = env.client.post(f"/api/chat/sessions/{pid}/side",
                        json={"content": "also handle the empty list case"}).json()
    assert r["action"] == "steer" and r["queued"] is True
    assert "waiting for a command to finish" in r["where"]
    assert "command_wait" not in r["where"]
    chat_interject.clear(pid)
    chat_runs.finish_all()


def test_the_status_endpoint(env, monkeypatch):
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    idle = env.store.create_session("idle", env.cwd)["id"]
    d = env.client.get(f"/api/chat/sessions/{idle}/status").json()
    assert d["running"] is False and "nothing is running" in d["text"]
    pid = env.parent()
    chat_runs.start(pid).publish({"type": "tool_start", "name": "grep",
                                  "args": {"pattern": "TODO"}, "call_id": 1})
    d = env.client.get(f"/api/chat/sessions/{pid}/status").json()
    assert d["running"] is True and "**Now:** `grep` `TODO`" in d["text"]
    assert d["snapshot"]["in_flight"][0]["name"] == "grep"
    assert env.client.get("/api/chat/sessions/9999/status").status_code == 404
    chat_runs.finish_all()


def test_the_side_agent_is_told_what_the_main_run_is_doing(env, monkeypatch):
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_ensure_watchdog", lambda: None)
    pid = env.parent()
    chat_runs.start(pid).publish({"type": "tool_start", "name": "write_file",
                                  "args": {"path": "src/tax.py"}, "call_id": 1})
    pre = env.st._context_preamble(env.store.get_session(pid))
    assert "Live status of that chat's running work" in pre
    assert "`write_file` `tax.py`" in pre
    chat_runs.finish_all()
    assert "Live status" not in env.st._context_preamble(env.store.get_session(pid))


def test_a_finished_task_carries_its_whole_answer_for_the_chat_to_show(env):
    pid = env.parent()
    t = env.st.create(pid, "what does the retry helper do?")
    long_answer = "It retries three times with backoff. " * 40
    env.running.discard(t["id"])
    env.store.add_message(t["id"], "assistant", long_answer)
    d = env.client.get(f"/api/chat/sessions/{pid}/tasks").json()["tasks"][0]
    # the main run is still going, so nothing is filed into its history yet…
    assert d["state"] == "done" and d["posted"] is False
    # …but the answer is available to show right away
    assert d["answer"] == long_answer.strip()
    assert len(d["preview"]) <= 240


# ── a request for information is its own task; a correction steers ────────────

@pytest.mark.parametrize("text", [
    "Answer these 2 questions: 1. what port does the shop API listen on 2. what is the first line of the README",
    "answer 4 questions about the schema, the AI interface, the algorithm and the destination tables",
    "1. schema of the first ClickHouse table\n2. is there a working AI interface\n3. is the algorithm proven",
    "explain how the retry works", "describe the deployment flow", "summarise the open TODOs",
    "list the failing tests", "tell me which services use redis", "show me the schema",
    "can you explain why the build is slow", "do we have a working AI interface",
    "why is it slow", "which tables hold the emitters?", "is there a Rust version yet?",
    "I need to understand how the clustering step decides which pulses belong together, "
    "what thresholds it uses, where those thresholds come from, and how they were tuned "
    "against the 50k run so I can explain it in tomorrow's review meeting with the team",
])
def test_a_request_for_information_is_its_own_task(text):
    assert __import__("aiforge_core.api.routes._chat._side_tasks",
                      fromlist=["x"]).classify(text) == "task"


@pytest.mark.parametrize("text", [
    "also handle the empty list case", "make the button blue", "use postgres instead",
    "no, name it apply_discount", "can you also log the date?", "don't touch the tests",
    "add a retry to client.py", "stop", "scratch that, do something else",
    "rename the helper function in utils.py", "go faster",
])
def test_a_correction_still_steers(text):
    assert __import__("aiforge_core.api.routes._chat._side_tasks",
                      fromlist=["x"]).classify(text) == "steer"
