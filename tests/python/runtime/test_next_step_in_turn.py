"""The prediction's place in a turn: last, optional, and never in the way."""
from __future__ import annotations

import subprocess
import types

from aiforge_core.runtime import next_step
from aiforge_core.runtime.chat_agent import _loop


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
    monkeypatch.setattr(_loop, "_predict_next_step", lambda *a, **k: prediction)
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

    monkeypatch.setattr(_loop, "_predict_next_step", _boom)
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
    monkeypatch.setattr(_loop, "_is_clean_tree", lambda cwd: calls.append(cwd) or False)
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
    monkeypatch.setattr(_loop, "_is_clean_tree", lambda *a, **k: True)

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
    monkeypatch.setattr(_loop, "_is_clean_tree", lambda *a, **k: True)

    evs = list(_loop._emit_suggestion(
        "In one short sentence: what does the run lock in chat_pipeline "
        "protect?", "explained it", "/repo"))

    assert evs == []
