"""Mem0/Letta write-path: extract → decide → apply → reflect (gaps #1,2,5)."""
from __future__ import annotations

import json

import pytest

from aiforge_memory.features.memory import consolidate as C
from aiforge_memory.features.memory import store


# ── extract ──────────────────────────────────────────────────────────
def test_extract_parses_json_array():
    llm = lambda s, u: json.dumps(["DECISION: use X", "gotcha: Y is null"])
    assert C.extract_facts("traj", llm_fn=llm) == [
        "DECISION: use X", "gotcha: Y is null"]


def test_extract_handles_fenced_json():
    llm = lambda s, u: "```json\n[\"a\"]\n```"
    assert C.extract_facts("traj", llm_fn=llm) == ["a"]


def test_extract_bad_output_is_empty():
    assert C.extract_facts("traj", llm_fn=lambda s, u: "not json") == []
    assert C.extract_facts("", llm_fn=lambda s, u: "[]") == []   # empty traj


def test_extract_soft_fails_on_llm_error():
    def boom(s, u): raise RuntimeError("llm down")
    assert C.extract_facts("traj", llm_fn=boom) == []


def test_extract_caps_max_facts():
    llm = lambda s, u: json.dumps([str(i) for i in range(20)])
    assert len(C.extract_facts("t", llm_fn=llm, max_facts=3)) == 3


# ── decide ───────────────────────────────────────────────────────────
def test_decide_no_similar_is_add_without_llm():
    def must_not_call(s, u): raise AssertionError("llm called")
    d = C.decide_action("new", [], llm_fn=must_not_call)
    assert d["action"] == "ADD"


def test_decide_update_with_target():
    llm = lambda s, u: json.dumps({"action": "UPDATE", "target": "obs1",
                                   "reason": "refines"})
    d = C.decide_action("new", [{"id": "obs1", "text": "old"}], llm_fn=llm)
    assert d["action"] == "UPDATE" and d["target"] == "obs1"


def test_decide_bad_action_defaults_add():
    llm = lambda s, u: json.dumps({"action": "WHATEVER", "target": "x"})
    assert C.decide_action("n", [{"id": "x", "text": "t"}],
                           llm_fn=llm)["action"] == "ADD"


def test_decide_soft_fails_to_add():
    def boom(s, u): raise RuntimeError("x")
    assert C.decide_action("n", [{"id": "x", "text": "t"}],
                           llm_fn=boom)["action"] == "ADD"


# ── reflect ──────────────────────────────────────────────────────────
def test_reflect_none_is_empty():
    assert C.reflect("t", llm_fn=lambda s, u: "NONE") == ""


def test_reflect_returns_text():
    assert C.reflect("t", llm_fn=lambda s, u: "learned X") == "learned X"


# ── apply_decision routing ───────────────────────────────────────────
@pytest.fixture
def captured(monkeypatch):
    calls = {"upsert": [], "invalidate": []}

    def fake_upsert(driver, **kw):
        calls["upsert"].append(kw)
        return {"id": "newid", "deduped": False}

    def fake_invalidate(driver, **kw):
        calls["invalidate"].append(kw)
        return {"id": kw["node_id"], "invalidated": True}

    monkeypatch.setattr(store, "upsert_observation", fake_upsert)
    monkeypatch.setattr(store, "invalidate_observation", fake_invalidate)
    return calls


def test_apply_add(captured):
    r = C.apply_decision(None, repo="r", fact="f",
                         decision={"action": "ADD", "target": ""},
                         embed_vec=None, author="learner")
    assert r["action"] == "ADD"
    assert captured["upsert"][0].get("supersedes") in (None,)


def test_apply_update_supersedes_target(captured):
    C.apply_decision(None, repo="r", fact="f",
                     decision={"action": "UPDATE", "target": "old1"},
                     embed_vec=None, author="learner")
    assert captured["upsert"][0]["supersedes"] == ["old1"]


def test_apply_delete_invalidates(captured):
    r = C.apply_decision(None, repo="r", fact="gone",
                         decision={"action": "DELETE", "target": "old2",
                                   "reason": "obsolete"},
                         embed_vec=None, author="learner")
    assert r == {"action": "DELETE", "id": "old2"}
    assert captured["invalidate"][0]["node_id"] == "old2"
    assert not captured["upsert"]            # DELETE writes no new node


def test_apply_decision_kind_decision_prefix(captured):
    C.apply_decision(None, repo="r", fact="DECISION: pick X",
                     decision={"action": "ADD", "target": ""},
                     embed_vec=None, author="learner")
    assert captured["upsert"][0]["kind"] == "decision"


# ── consolidate end-to-end (mocked) ──────────────────────────────────
def test_consolidate_runs_extract_decide_apply_reflect(monkeypatch):
    monkeypatch.setattr(store, "recall_observations", lambda *a, **k: [])
    writes = []
    monkeypatch.setattr(store, "upsert_observation",
                        lambda driver, **kw: writes.append(kw) or {"id": "x"})

    def llm(system, user):
        if "distil DURABLE" in system:
            return json.dumps(["fact A", "DECISION: B"])
        if "memory store" in system:
            return json.dumps({"action": "ADD", "target": ""})
        return "reflection summary"   # reflect

    out = C.consolidate(None, repo="r", trajectory_text="did stuff",
                        llm_fn=llm, embed_fn=lambda t: [0.1] * 8)
    assert out["facts"] == 2
    assert len(out["actions"]) == 2
    assert out["reflection"] == "x"
    # 2 facts + 1 reflection written
    assert len(writes) == 3
    assert any(w.get("kind") == "reflection" for w in writes)
