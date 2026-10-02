"""The handoff is saved with the chat and the next turn resumes from it.

Work must survive a crash, a restart, a Stop+retry and a new message in the same
chat; and a fresh unrelated request must not inherit any of it.
"""
import json
import threading
import types as pytypes

import pytest

from aiforge_core.runtime import chat_store, handoff, handoff_store as H


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.delenv("AIFORGE_CHAT_HANDOFF_PERSIST", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_HANDOFF_EVERY", raising=False)
    chat_store.reset_backend_for_tests()
    yield tmp_path
    chat_store.reset_backend_for_tests()


def _sid():
    return chat_store.create_session("t")["id"]


def _rec(**kw):
    base = {"goal": "make the read path under 500 ms", "done": ["profile it"],
            "open": ["add the index"], "files": ["/w/read.py"],
            "failed": ["python bench.py -> ValueError: bad shape"],
            "error": "ValueError: bad shape", "status": "stopped"}
    base.update(kw)
    return base


# ── write / read, bounded, atomic ───────────────────────────────────────

def test_round_trip():
    sid = _sid()
    assert H.load(sid) is None
    assert H.save(sid, _rec())
    got = H.load(sid)
    assert got["goal"] == "make the read path under 500 ms"
    assert got["done"] == ["profile it"] and got["open"] == ["add the index"]
    assert got["failed"] == ["python bench.py -> ValueError: bad shape"]
    assert got["status"] == "stopped" and got["updated_at"] > 0
    H.clear(sid)
    assert H.load(sid) is None


def test_the_record_is_bounded():
    sid = _sid()
    big = _rec(goal="g" * 50_000, done=[f"item {i} " + "x" * 500 for i in range(500)],
               files=[f"/w/{i}/" + "y" * 400 for i in range(500)],
               failed=["f" * 5000] * 50, error="e" * 9000)
    assert H.save(sid, big)
    raw = chat_store.get_session_handoff(sid)
    assert len(raw.encode()) <= H.MAX_BYTES
    got = H.load(sid)
    assert len(got["failed"]) <= handoff.MAX_FAILED and got["goal"]


def test_a_failed_write_leaves_the_old_record(monkeypatch):
    sid = _sid()
    H.save(sid, _rec(goal="old goal"))

    def boom(*a, **k):
        raise RuntimeError("disk full")
    with monkeypatch.context() as m:
        m.setattr(chat_store, "set_session_handoff", boom)
        assert H.save(sid, _rec(goal="new goal")) is False
    assert H.load(sid)["goal"] == "old goal"


def test_a_damaged_record_reads_as_none():
    sid = _sid()
    chat_store.set_session_handoff(sid, '{"v": 1, "goal": "x"')       # cut off
    assert H.load(sid) is None
    chat_store.set_session_handoff(sid, json.dumps({"v": 99}))
    assert H.load(sid) is None


def test_concurrent_writers_never_leave_half_a_record():
    sid = _sid()
    stop = threading.Event()
    seen = []

    def writer(tag):
        for i in range(25):
            H.save(sid, _rec(goal=f"{tag}-{i}", done=[f"{tag}{j}" for j in range(30)]))

    def reader():
        while not stop.is_set():
            raw = chat_store.get_session_handoff(sid)
            if raw:
                seen.append(json.loads(raw))            # raises on a torn write
    ts = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    r = threading.Thread(target=reader)
    r.start()
    [t.start() for t in ts]
    [t.join() for t in ts]
    stop.set()
    r.join()
    assert seen and all(x["v"] == 1 for x in seen)


def test_deleting_the_chat_deletes_the_record():
    sid = _sid()
    H.save(sid, _rec())
    chat_store.delete_session(sid)
    assert H.load(sid) is None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_HANDOFF_PERSIST", "0")
    sid = _sid()
    assert H.save(sid, _rec()) is False
    assert chat_store.get_session_handoff(sid) is None
    rows = [{"role": "user", "content": "go"}]
    history = [{"role": "user", "content": "go"}]
    assert H.seed_next_turn(sid, rows, "go", history) == ""


# ── the loop saves it ───────────────────────────────────────────────────

def _scripted(outputs, hook=None):
    seq = list(outputs)

    def fn(role, messages, **kw):
        if hook:
            hook(len(outputs) - len(seq))
        return seq.pop(0)
    return fn


def _plan(slug, title, status):
    return ("ACTION: plan_progress\nARGS_JSON: "
            + json.dumps({"slug": slug, "title": title, "status": status}))


def _run(sid, tmp_path, outputs, hook=None, take=None):
    from aiforge_core.runtime import chat_agent as ca
    gen = ca.run_chat_agent(
        [{"role": "user", "content": "refactor the parser and add the cache"}],
        cwd=str(tmp_path), complete_fn=_scripted(outputs, hook), session_id=sid)
    evs = []
    for ev in gen:
        evs.append(ev)
        if take and len(evs) >= take:
            gen.close()                                  # the process died here
            break
    return evs


def test_a_closed_run_leaves_an_interrupted_record_with_its_board(tmp_path):
    sid = _sid()
    _run(sid, tmp_path, [_plan("parser", "refactor the parser", "done"),
                         _plan("cache", "add the cache", "running"),
                         "FINAL: done"], take=6)
    got = H.load(sid)
    assert got["status"] == "interrupted"
    assert "refactor the parser" in got["done"]
    assert "add the cache" in got["open"]
    assert "refactor the parser and add the cache" in got["goal"]


def test_a_finished_turn_leaves_nothing_to_resume(tmp_path):
    sid = _sid()
    H.save(sid, _rec())                                   # an earlier unfinished one
    _run(sid, tmp_path, ["FINAL: all done"])
    assert H.load(sid) is None


def test_stop_is_recorded(tmp_path):
    from aiforge_core.runtime import chat_cancel
    sid = _sid()
    chat_cancel.start(sid)

    def hook(i):
        if i == 1:
            chat_cancel.cancel(sid)
    try:
        evs = _run(sid, tmp_path, [_plan("parser", "refactor the parser", "done"),
                                   "FINAL: x"], hook=hook)
    finally:
        chat_cancel.finish(sid)
    assert any(e.get("text") == "stopped by user" for e in evs)
    got = H.load(sid)
    assert got["status"] == "stopped" and "refactor the parser" in got["done"]


def test_a_closed_item_saves_at_once(tmp_path, monkeypatch):
    sid = _sid()
    reasons = []
    real = H.save
    monkeypatch.setattr(H, "save", lambda s, h: (reasons.append(h.get("reason")), real(s, h))[1])
    _run(sid, tmp_path, [_plan("a", "first thing", "pending"),
                         _plan("a", "", "done"), "FINAL: ok"], take=6)
    assert "item_done" in reasons


def test_every_n_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_HANDOFF_EVERY", "2")
    sid = _sid()
    reasons = []
    real = H.save
    monkeypatch.setattr(H, "save", lambda s, h: (reasons.append(h.get("reason")), real(s, h))[1])
    outs = [_plan(f"s{i}", f"step {i}", "pending") for i in range(5)]
    _run(sid, tmp_path, outs + ["FINAL: ok"], take=16)
    assert reasons.count("steps") >= 2


def test_a_stuck_restart_is_saved(tmp_path):
    from aiforge_core.runtime.chat_agent._turn import _escalate as E
    sid = _sid()
    st = pytypes.SimpleNamespace(
        convo=[{"role": "system", "content": "S"},
               {"role": "user", "content": "fix it"},
               {"role": "assistant", "content": 'ACTION: run_command\nARGS_JSON: {"cmd": "x"}'},
               {"role": "user", "content": "OBSERVATION: Traceback\nValueError: bad"}],
        role="doer", goal="fix it", board={}, file_hashes={}, session_id=sid,
        read_sigs_seen=set(), recent_outputs=[], steers=[], builder="")
    rec = H.Recorder(st)
    try:
        assert E.restart_with_handoff(st)
    finally:
        rec.finish()
    got = H.load(sid)
    assert got["goal"] == "fix it"
    assert got["offload"]                     # the transcript it replaced
    assert "ValueError" in got["error"]       # and the error it was stuck on


# ── resume ──────────────────────────────────────────────────────────────

def _unfinished_chat(prompt="refactor the parser and add the cache"):
    sid = _sid()
    chat_store.add_message(sid, "user", "what does this repo do?")
    chat_store.add_message(sid, "assistant", "It parses logs.")
    chat_store.add_message(sid, "user", prompt)
    chat_store.add_message(sid, "assistant", "(stopped: hit the runaway safety cap)",
                           steps=[{"type": "tool", "name": "run_command",
                                   "args": {"cmd": "pytest"}, "result": {"ok": False}},
                                  {"type": "stopped", "reason": "cancelled"}])
    H.save(sid, _rec(goal=prompt, turn_prompt=prompt))
    return sid


def _next(sid, text, resume=None):
    from aiforge_core.api.routes._chat._history import _chat_history_for_agent
    from aiforge_core.api.routes._chat._prep import _apply_resume_brief
    chat_store.add_message(sid, "user", text)
    rows = chat_store.get_messages(sid)
    history = _chat_history_for_agent(rows)
    brief = _apply_resume_brief(rows, text, "/repo",
                                pytypes.SimpleNamespace(resume=resume), history, sid)
    return brief, history


def test_continue_after_stop_seeds_the_handoff_not_the_raw_history():
    sid = _unfinished_chat()
    brief, history = _next(sid, "continue")
    assert "[HANDOFF" in brief and "ALREADY TRIED AND FAILED" in brief
    assert "ValueError: bad shape" in brief and "NEXT: add the index" in brief
    last = history[-1]
    assert last["role"] == "user" and last["content"].startswith("continue\n\n---\n[HANDOFF")
    joined = "\n".join(m["content"] for m in history)
    assert "(stopped: hit the runaway" not in joined          # raw tail replaced
    assert "what does this repo do?" in joined                # earlier turns kept
    # the original request is quoted so the loop knows its goal after "continue"
    from aiforge_core.runtime.chat_agent._turn._state import _turn_goal
    assert _turn_goal(history) == "refactor the parser and add the cache"
    # raw history stays available through the offload id
    from aiforge_core.runtime import context_offload
    oid = H.load(sid)["offload"]
    assert oid and "(stopped: hit the runaway" in context_offload.load(oid)["text"]
    assert f'"id": "{oid}"' in brief


def test_retry_with_the_same_words_resumes():
    sid = _unfinished_chat()
    brief, _ = _next(sid, "refactor the parser and add the cache")
    assert "[HANDOFF" in brief


def test_a_crash_with_no_assistant_row_resumes():
    sid = _sid()
    chat_store.add_message(sid, "user", "migrate the billing tables to postgres")
    H.save(sid, _rec(goal="migrate the billing tables to postgres",
                     status="running", turn_prompt="migrate the billing tables to postgres"))
    brief, history = _next(sid, "continue")
    assert "[HANDOFF" in brief
    assert len(history) == 1 and history[0]["role"] == "user"


def test_a_steer_about_the_same_goal_resumes():
    sid = _unfinished_chat()
    brief, _ = _next(sid, "also keep the parser backwards compatible")
    assert "[HANDOFF" in brief


def test_a_fresh_unrelated_request_does_not_inherit_and_clears():
    sid = _unfinished_chat()
    brief, history = _next(sid, "write a haiku about autumn rain")
    assert brief == "" and "HANDOFF" not in history[-1]["content"]
    assert H.load(sid) is None


def test_an_explicit_new_task_clears_even_with_shared_words():
    sid = _unfinished_chat()
    brief, _ = _next(sid, "new task: refactor the billing parser")
    assert brief == "" and H.load(sid) is None


def test_a_question_neither_resumes_nor_loses_the_work():
    sid = _unfinished_chat()
    brief, _ = _next(sid, "why did the benchmark fail?")
    assert brief == ""
    assert H.load(sid) is not None


def test_a_clean_rerun_drops_it():
    sid = _unfinished_chat()
    brief, _ = _next(sid, "continue", resume=False)
    assert brief == "" and H.load(sid) is None


def test_a_stale_record_is_not_inherited(monkeypatch):
    sid = _unfinished_chat()
    raw = json.loads(chat_store.get_session_handoff(sid))
    raw["updated_at"] -= 30 * 86400
    chat_store.set_session_handoff(sid, json.dumps(raw))
    brief, _ = _next(sid, "continue")
    assert brief == "" and H.load(sid) is None


def test_the_resumed_run_carries_the_failures_forward(tmp_path):
    sid = _unfinished_chat()
    brief, history = _next(sid, "continue")
    from aiforge_core.runtime import chat_agent as ca
    seen = {}

    def fn(role, messages, **kw):
        seen.setdefault("first", str(messages[-1].get("content")))
        return _plan("x", "keep going", "running")
    # keep the run unfinished so the record is rewritten at its end
    gen = ca.run_chat_agent(history, cwd=str(tmp_path), complete_fn=fn, session_id=sid)
    for _ev in gen:
        if "first" in seen:
            break
    gen.close()
    assert "ALREADY TRIED" in seen["first"]
    got = H.load(sid)
    assert "python bench.py -> ValueError: bad shape" in got["failed"]
    assert "profile it" in got["done"]


# ── pipeline ────────────────────────────────────────────────────────────

def test_ticket_run_keeps_failed_approaches_across_a_resume(monkeypatch):
    from aiforge_core.runtime.graph_pipeline import _gates as G
    patches = []
    from aiforge_core.tickets import store as tickets_mod
    monkeypatch.setattr(tickets_mod, "patch_fields",
                        lambda tid, fields=None, metadata_patch=None:
                        patches.append((tid, metadata_patch)))
    state = {"_ticket_id": 41, "raw_ask": "fix the flaky sync", "doer_iters": 2}
    G._note_failed_approach(state, "pass 1: the tests failed — KeyError 'id'")
    assert patches and patches[-1][0] == 41
    meta = patches[-1][1]
    assert meta["failed_approaches"] == ["pass 1: the tests failed — KeyError 'id'"]
    assert meta["doer_handoff"]["goal"] == "fix the flaky sync"

    # the run dies; the retry is seeded from the ticket
    ticket = pytypes.SimpleNamespace(id=41, identifier="ONE-1", project="p",
                                     title="fix the flaky sync", body="",
                                     metadata=meta)
    from aiforge_core.runtime.adk_runner import _run_inputs as RI
    monkeypatch.setattr(RI, "_collect_repo_rules", lambda *a, **k: "")
    seeded = RI._ticket_state(ticket, [], "", "")
    assert seeded["failed_approaches"] == meta["failed_approaches"]
    assert "KeyError" in seeded["failed_approaches_md"]
    assert seeded["doer_handoff"]["goal"] == "fix the flaky sync"
    from aiforge_core.runtime import text_doer_seed as S
    assert any(k == "failed_approaches_md" for k, _ in S._SEED_VARS)
    # ...and a further failure appends rather than starts over
    G._note_failed_approach(seeded, "pass 2: no edit")
    assert len(seeded["failed_approaches"]) == 2


def test_the_final_ticket_metadata_carries_the_handoff():
    from aiforge_core.runtime.adk_runner._outcome import _handoff_patch
    state = {"failed_approaches": ["a", "b"], "raw_ask": "goal", "_iter_fail": ["x", 1, "boom"]}
    patch = _handoff_patch(state)
    assert patch["failed_approaches"] == ["a", "b"]
    assert patch["doer_handoff"]["error"] == "boom"
    assert _handoff_patch({}) == {}


def test_a_team_run_keeps_them_in_the_chat_record():
    sid = _sid()
    H.save(sid, _rec(status="stopped"))
    H.note_team_state(sid, {"failed_approaches": ["pass 1: tests failed"],
                            "raw_ask": "build x"})
    seed = H.team_seed(sid)
    assert "pass 1: tests failed" in seed["failed_approaches"]
    assert "pass 1: tests failed" in seed["failed_approaches_md"]
    H.close_team_turn(sid, "build x", [{"slug": "a", "goal": "g", "status": "done"}],
                      True, False)
    assert H.load(sid) is None                                  # finished: cleared
    H.close_team_turn(sid, "build x", [{"slug": "a", "goal": "g", "status": "running"}],
                      False, True)
    assert H.load(sid)["status"] == "stopped"


# ── seeing it ───────────────────────────────────────────────────────────

def test_status_answer_says_where_it_stands():
    from aiforge_core.runtime import chat_status
    lines = chat_status.handoff_lines(H.bound(_rec()))
    text = "\n".join(lines)
    assert "profile it" in text and "add the index" in text
    assert "ValueError: bad shape" in text


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aiforge_core.api.routes import chat as C
    app = FastAPI()
    app.include_router(C.router)
    return TestClient(app)


def test_handoff_endpoint_and_stop(client):
    sid = _sid()
    r = client.get(f"/api/chat/sessions/{sid}/handoff")
    assert r.status_code == 200 and r.json()["handoff"] is None
    H.save(sid, _rec(status="running"))
    r = client.get(f"/api/chat/sessions/{sid}/handoff").json()
    assert r["unfinished"] and r["handoff"]["goal"] and "NEXT:" in r["text"]
    assert client.post(f"/api/chat/sessions/{sid}/stop").status_code == 200
    assert H.load(sid)["status"] == "stopped"
    assert client.get("/api/chat/sessions/99999/handoff").status_code == 404


def test_idle_status_mentions_unfinished_work(client):
    sid = _sid()
    H.save(sid, _rec())
    text = client.get(f"/api/chat/sessions/{sid}/status").json()["text"]
    assert "ended unfinished" in text and "add the index" in text
