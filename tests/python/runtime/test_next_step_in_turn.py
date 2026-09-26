"""The prediction's place in a turn: last, optional, and never in the way."""
from __future__ import annotations

import subprocess
import threading
import time
import types
import uuid

from aiforge_core.runtime import next_step
from aiforge_core.runtime.chat_agent import _loop
from aiforge_core.runtime.chat_agent._turn import _finish


def _prediction(verdict=next_step.ACT, action="check it", tool="read_file"):
    # A NAMED tool by default: `_risk` will not hand back ACT for a prediction
    # that names none, so a toolless ACT is a combination `predict` can no
    # longer produce, and a fixture that ships one tests a shape that cannot
    # reach this code.
    return next_step.Prediction(id="p-1", action=action, tool=tool,
                                args={"path": "x/y.py"},
                                confidence=0.9, rationale="a host was named",
                                verdict=verdict)


def _events(monkeypatch, prediction):
    monkeypatch.setattr(_finish, "_predict_next_step", lambda *a, **k: prediction)
    return list(_loop._emit_suggestion("hello", "read_file", "/repo"))


# ── the event ────────────────────────────────────────────────────────────

def test_a_prediction_becomes_one_suggestion_event(monkeypatch):
    evs = _events(monkeypatch, _prediction())
    assert [e["type"] for e in evs] == ["suggestion"]
    assert evs[0]["action"] == "check it"
    assert evs[0]["verdict"] == "ACT"
    assert evs[0]["id"] == "p-1"


def test_no_prediction_emits_nothing(monkeypatch):
    assert _events(monkeypatch, None) == []


def test_a_raising_predictor_emits_nothing_and_does_not_propagate(monkeypatch):
    """A prediction never breaks a turn — the answer is already out."""
    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(_finish, "_predict_next_step", _boom)
    assert list(_loop._emit_suggestion("hello", "did a thing", "/repo")) == []


def test_the_event_carries_no_argument_values(monkeypatch):
    p = next_step.Prediction(id="p-2", action="connect", tool="bash",
                             args={"cmd": "psql postgres://u:p4ssw0rd@db/x"},
                             confidence=0.9, rationale="x",
                             verdict=next_step.OFFER)
    assert "p4ssw0rd" not in str(_events(monkeypatch, p))


# ── clean_tree, the tier-2 gate ──────────────────────────────────────────

def test_a_non_git_directory_is_never_reported_clean(tmp_path):
    """_worktree_fingerprint returns "" for BOTH a clean tree and a non-repo,
    and its docstring warns "" means no signal. Reusing it here would let a
    workspace-writing prediction act where there is no undo at all."""
    assert _loop._is_clean_tree(str(tmp_path)) is False


def test_a_clean_repo_is_reported_clean(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert _loop._is_clean_tree(str(tmp_path)) is True


def test_a_dirty_repo_is_not_reported_clean(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("uncommitted", encoding="utf-8")
    assert _loop._is_clean_tree(str(tmp_path)) is False


def test_an_empty_cwd_is_not_clean():
    assert _loop._is_clean_tree("") is False
    assert _loop._is_clean_tree(None) is False


# ── what goes into the prompt ────────────────────────────────────────────

def test_the_turn_summary_names_the_tools_that_ran():
    st = types.SimpleNamespace(action_counts={"read_file": 2, "grep": 1,
                                              "write_file": 0})
    summary = _loop._turn_summary(st)
    assert "read_file" in summary
    assert "grep" in summary
    assert "write_file" not in summary, "a tool that never ran is not what we did"


def test_the_turn_summary_survives_a_missing_tally():
    assert _loop._turn_summary(types.SimpleNamespace()) == ""


def test_the_last_user_message_is_what_is_predicted_from():
    st = types.SimpleNamespace(convo=[
        {"role": "user", "content": "first thing"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "connect to db.internal"},
    ])
    assert _loop._last_user_message(st) == "connect to db.internal"


def test_a_conversation_with_no_user_turn_is_not_an_error():
    assert _loop._last_user_message(types.SimpleNamespace(convo=[])) == ""
    assert _loop._last_user_message(types.SimpleNamespace()) == ""


# ── accept / dismiss ─────────────────────────────────────────────────────

def _api(monkeypatch, tmp_path):
    import importlib

    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN",
              "AIFORGE_BIND_HOST"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient
    return TestClient(api.app)


def _pending(pid="p-9"):
    from aiforge_core.runtime.next_step import _store

    _store.remember(_prediction(verdict=next_step.OFFER), {
        "repo": "R", "message": "connect to the database"})
    rows = _store._read()
    rows[-1]["id"] = pid
    _store._write(rows)


def test_accepting_records_the_outcome(monkeypatch, tmp_path):
    client = _api(monkeypatch, tmp_path)
    _pending("p-9")

    r = client.post("/api/chat/suggestion/p-9", json={"accepted": True})

    assert r.status_code == 200
    assert next_step.history(5)[0]["accepted"] is True


def test_dismissing_records_it_too(monkeypatch, tmp_path):
    """A feature that learns only from its wins drifts."""
    client = _api(monkeypatch, tmp_path)
    _pending("p-10")

    client.post("/api/chat/suggestion/p-10", json={"accepted": False})

    assert next_step.history(5)[0]["accepted"] is False


def test_an_unknown_id_is_not_an_error(monkeypatch, tmp_path):
    """A stale chip in a browser tab left open across a restart must not 500."""
    client = _api(monkeypatch, tmp_path)
    assert client.post("/api/chat/suggestion/p-gone",
                       json={"accepted": True}).status_code == 200


def test_a_bodyless_click_is_treated_as_a_dismissal(monkeypatch, tmp_path):
    client = _api(monkeypatch, tmp_path)
    _pending("p-11")

    r = client.post("/api/chat/suggestion/p-11")

    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_the_history_route_reports_the_counters(monkeypatch, tmp_path):
    """The numbers that answer 'is this good enough to extend to the pipeline'."""
    client = _api(monkeypatch, tmp_path)
    _pending("p-12")
    _pending("p-13")
    client.post("/api/chat/suggestion/p-12", json={"accepted": True})
    client.post("/api/chat/suggestion/p-13", json={"accepted": False})

    row = client.get("/api/chat/suggestions").json()

    assert row["accepted"] == 1
    assert row["dismissed"] == 1
    assert len(row["suggestions"]) == 2


def test_the_history_limit_is_bounded(monkeypatch, tmp_path):
    client = _api(monkeypatch, tmp_path)
    assert client.get("/api/chat/suggestions?limit=99999").status_code == 200
    assert client.get("/api/chat/suggestions?limit=0").status_code == 200


def test_the_kill_switch_costs_nothing_not_merely_emits_nothing(monkeypatch):
    """_is_clean_tree shells out to git on every turn end. Building the
    prediction context before honouring the switch meant a disabled feature
    still paid for a subprocess per turn."""
    calls = []
    monkeypatch.setattr(_finish, "_is_clean_tree", lambda cwd: calls.append(cwd) or False)
    monkeypatch.setenv("AIFORGE_PREDICT_DISABLE", "1")

    assert list(_loop._emit_suggestion("hi", "read_file", "/repo")) == []
    assert calls == []


# ── the echo, all the way out to the wire ────────────────────────────────

def test_a_toolless_prediction_reaches_the_wire_as_an_offer(monkeypatch, tmp_path):
    """End to end, the shape that re-ran a user's own question. The chip
    auto-sends an ACT, so the verdict on the wire is the whole difference
    between a suggestion and a second turn."""
    from aiforge_core.runtime.next_step import _predict as _np

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        _np, "_llm",
        lambda *a, **k: '{"action":"pears and the price of them","tool":"",'
                        '"args":{},"confidence":0.99,"rationale":"x"}')
    monkeypatch.setattr(_finish, "_is_clean_tree", lambda *a, **k: True)

    evs = list(_loop._emit_suggestion("what is a pear", "answered", "/repo"))

    assert [e["type"] for e in evs] == ["suggestion"]
    assert evs[0]["verdict"] == "OFFER"


def test_an_echo_emits_no_event_at_all(monkeypatch, tmp_path):
    """Not even an offer: the chip is meant to mean something."""
    from aiforge_core.runtime.next_step import _predict as _np

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        _np, "_llm",
        lambda *a, **k: '{"action":"Explain what the run lock in chat_pipeline '
                        'protects.","tool":"read_file","args":{"path":"x"},'
                        '"confidence":0.95,"rationale":"x"}')
    monkeypatch.setattr(_finish, "_is_clean_tree", lambda *a, **k: True)

    evs = list(_loop._emit_suggestion(
        "In one short sentence: what does the run lock in chat_pipeline "
        "protect?", "explained it", "/repo"))

    assert evs == []


# ── off the critical path: the answer and `done` never wait on it ───────

def _timing(monkeypatch, grace):
    """Every timing test pins its own grace and runs with the feature ON."""
    monkeypatch.setenv("AIFORGE_PREDICT_GRACE_S", str(grace))
    monkeypatch.delenv("AIFORGE_PREDICT_AFTER_DONE_S", raising=False)
    monkeypatch.delenv("AIFORGE_PREDICT_DISABLE", raising=False)


def _unique(**kw):
    return next_step.Prediction(**{**dict(
        id=f"p-{uuid.uuid4().hex[:8]}", action="check it", tool="read_file",
        args={}, confidence=0.9, rationale="x", verdict=next_step.OFFER), **kw})


def _stored_ids():
    from aiforge_core.runtime.next_step import _store
    return {r.get("id") for r in _store._read()}


def _final_st():
    return types.SimpleNamespace(
        board_used=False, board={}, edits_made=0, readonly_mode=False,
        action_counts={"read_file": 1},
        convo=[{"role": "user", "content": "hello"}])


def _final_gen(monkeypatch, tmp_path):
    monkeypatch.setattr(_finish, "_fire_stop", lambda *a, **k: None)
    # These tests are about the prediction's timing, on a server with a spare
    # slot (a one-slot server skips the prediction: test_parallel_slots_turn).
    monkeypatch.setattr(_finish, "_endpoint_one_slot", lambda: False)
    return _finish._handle_final(_final_st(), {"type": "final", "text": "x"},
                                 None, False, False, False, str(tmp_path), [], "")


def _blocks_until_cancelled(seen):
    """A predictor that behaves like an LLM call honouring its cancel token."""
    from aiforge_core.llm.client import _http

    def _pred(*a, **k):
        ev = _http._CANCEL.get()
        seen["token"] = ev
        seen["cancelled"] = bool(ev is not None and ev.wait(10))
        seen["over"] = True
        return seen["p"]
    return _pred


def _wait_for(seen, key="over", s=5.0):
    t0 = time.monotonic()
    while key not in seen and time.monotonic() - t0 < s:
        time.sleep(0.01)
    return key in seen


def test_a_slow_prediction_does_not_hold_done_back_and_is_cancelled(
        monkeypatch, tmp_path):
    """The enhancer can take ~20 s; the turn ends after the grace, and the
    dropped call is aborted instead of holding a one-slot local model."""
    _timing(monkeypatch, 0.2)
    seen = {"p": _unique()}
    monkeypatch.setattr(_finish, "_predict_next_step", _blocks_until_cancelled(seen))
    t0 = time.monotonic()
    evs = list(_final_gen(monkeypatch, tmp_path))
    assert time.monotonic() - t0 < 2
    assert [e["type"] for e in evs] == ["message", "done"]
    assert _wait_for(seen)
    assert seen["cancelled"] is True, "the LLM call must see the cancellation"
    assert seen["p"].id not in _stored_ids(), "never shown, so never offered"


def test_closing_the_turn_mid_answer_cancels_the_prediction(monkeypatch, tmp_path):
    _timing(monkeypatch, 5)
    seen = {"p": _unique()}
    monkeypatch.setattr(_finish, "_predict_next_step", _blocks_until_cancelled(seen))
    gen = _final_gen(monkeypatch, tmp_path)
    assert next(gen)["type"] == "message"
    gen.close()
    assert _wait_for(seen)
    assert seen["cancelled"] is True


def test_a_late_prediction_is_not_recorded_as_offered(monkeypatch):
    """Never shown, so it must not suppress the same suggestion next turn."""
    _timing(monkeypatch, 0.05)
    seen = {"p": _unique()}
    monkeypatch.setattr(_finish, "_predict_next_step", _blocks_until_cancelled(seen))
    assert list(_loop._emit_suggestion("hello", "read_file", "/repo")) == []
    assert _wait_for(seen)
    time.sleep(0.05)
    assert seen["p"].id not in _stored_ids()


def test_a_timely_prediction_is_emitted_between_answer_and_done(monkeypatch, tmp_path):
    _timing(monkeypatch, 5)
    p = _unique()
    monkeypatch.setattr(_finish, "_predict_next_step", lambda *a, **k: p)
    evs = list(_final_gen(monkeypatch, tmp_path))
    # done goes out first (the answer is complete); the suggestion follows.
    assert [e["type"] for e in evs] == ["message", "done", "suggestion"]
    assert evs[2]["id"] == p.id
    assert p.id in _stored_ids(), "an emitted suggestion is recorded as offered"


def test_the_prediction_runs_while_the_answer_is_consumed(monkeypatch, tmp_path):
    """Started when the answer is accepted, not after it is handed over: time
    the consumer spends on the message comes off the grace."""
    _timing(monkeypatch, 5)
    started = threading.Event()

    def _pred(*a, **k):
        started.set()
        return _unique()

    monkeypatch.setattr(_finish, "_predict_next_step", _pred)
    gen = _final_gen(monkeypatch, tmp_path)
    assert next(gen)["type"] == "message"
    assert started.wait(5), "prediction must already be running"
    assert [e["type"] for e in gen] == ["done", "suggestion"]


def test_the_grace_is_env_tunable_and_survives_garbage(monkeypatch):
    """The grace runs AFTER done and is short (the answer is already out; the
    prediction has had the answer's whole time too): 3 s by default, not the
    prediction's 10 s timeout that kept the stream open after done."""
    for key in ("AIFORGE_PREDICT_GRACE_S", "AIFORGE_PREDICT_AFTER_DONE_S",
                "AIFORGE_PREDICT_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    assert _finish._suggest_grace_s() == 3.0
    monkeypatch.setenv("AIFORGE_PREDICT_TIMEOUT_S", "25")
    assert _finish._suggest_grace_s() == 3.0
    monkeypatch.setenv("AIFORGE_PREDICT_GRACE_S", "0.3")      # the older name
    assert _finish._suggest_grace_s() == 0.3
    monkeypatch.setenv("AIFORGE_PREDICT_AFTER_DONE_S", "1.5")
    assert _finish._suggest_grace_s() == 1.5
    monkeypatch.setenv("AIFORGE_PREDICT_AFTER_DONE_S", "soon")
    assert _finish._suggest_grace_s() == 0.3
    monkeypatch.setenv("AIFORGE_PREDICT_GRACE_S", "-4")
    assert _finish._suggest_grace_s() == 0.0


def test_stop_or_a_new_message_ends_the_after_done_wait(monkeypatch):
    """The wait after done is Stop-aware and ends on a new message too."""
    import time as _t

    from aiforge_core.runtime import run_interrupt
    from aiforge_core.runtime.chat_agent._turn._suggest_wait import await_ready
    ev = threading.Event()
    monkeypatch.setattr(run_interrupt, "attention", lambda sid: "steer")
    t0 = _t.monotonic()
    assert await_ready(ev, 77, grace_s=5.0) is False
    assert _t.monotonic() - t0 < 1.0


def test_the_clean_tree_probe_takes_no_optional_locks(monkeypatch):
    """It runs on a side thread while the next turn may be writing."""
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(_finish.subprocess, "run", _run)
    assert _finish._is_clean_tree("/repo") is True
    assert "--no-optional-locks" in calls[0]


# ── store=False: only what is shown is recorded ──────────────────────────

def _raw_row(pid):
    return {"id": pid, "action": "open the deploy log for the api service",
            "tool": "read_file", "args": {"path": "deploy.log"},
            "confidence": 0.99, "rationale": "x"}


def test_predict_without_store_writes_no_row(monkeypatch):
    from aiforge_core.runtime.next_step import _predict as _np

    monkeypatch.setenv("AIFORGE_PREDICT_REPEAT_H", "0")
    pid = f"p-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(_np, "raw_prediction", lambda ctx: _raw_row(pid))
    p = next_step.predict({"message": "why did the build fail", "repo": "R"},
                          store=False)
    assert p is not None and p.id == pid
    assert pid not in _stored_ids()


def test_remember_writes_the_row(monkeypatch):
    pid = f"p-{uuid.uuid4().hex[:8]}"
    next_step.remember(_unique(id=pid), {"message": "why did the build fail",
                                         "repo": "R"})
    assert pid in _stored_ids()
