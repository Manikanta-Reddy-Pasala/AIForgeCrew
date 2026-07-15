"""OKR-DAG P1 — node schema + typed store (folders, ids, edges)."""
from __future__ import annotations

import tempfile

import pytest

from aiforge_core.memory import okr


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())


def test_render_parse_roundtrip_and_edges():
    md = okr.render_node("key_result", "KR-01",
                         {"parent_objective": "O-01", "title": "backtest",
                          "status": "in-progress", "metrics": {"cagr": "15%"}},
                         body="# Requirements\n- survivorship-bias-free")
    p = okr.parse_node(md)
    assert p["type"] == "key_result" and p["id"] == "KR-01"
    assert p["meta"]["parent_objective"] == "O-01"
    assert p["meta"]["metrics"]["cagr"] == "15%"
    assert "survivorship-bias-free" in p["body"]
    assert ("parent", "KR-01", "O-01") in okr.edges_of(p)


def test_learning_scope_edges():
    glob = okr.parse_node(okr.render_node("learning", "L-01",
                          {"scope": "global", "category": "devops"}, "no k8s"))
    assert okr.edges_of(glob) == []                       # global → no edge
    scoped = okr.parse_node(okr.render_node("learning", "L-02",
                            {"scope": ["O-01"]}, "survivorship bias"))
    assert ("scopes", "L-02", "O-01") in okr.edges_of(scoped)


def test_validate_requires_edges():
    bad = okr.parse_node(okr.render_node("key_result", "KR-9", {}, "x"))
    assert "key_result requires 'parent_objective'" in okr.validate(bad)
    good = okr.parse_node(okr.render_node("key_result", "KR-9",
                          {"parent_objective": "O-1"}, "x"))
    assert okr.validate(good) == []


def test_store_save_load_and_id_allocation(cfg):
    r1 = okr.save_node("objective", None, {"title": "Stock engine"}, "context")
    r2 = okr.save_node("objective", None, {"title": "Other"}, "ctx")
    assert r1["id"] == "O-01" and r2["id"] == "O-02"       # auto-increment
    kr = okr.save_node("key_result", None, {"parent_objective": "O-01"}, "kr")
    assert kr["id"] == "KR-01"
    all_nodes = {n["id"]: n for n in okr.load_all()}
    assert set(all_nodes) == {"O-01", "O-02", "KR-01"}
    assert all_nodes["O-01"]["type"] == "objective"
    assert all_nodes["O-01"]["meta"]["timestamp"]        # objective stamped (OKF)
    # a session id is date-based + unique
    s1 = okr.save_node("session", None, {"linked_krs": ["KR-01"]}, "ran x")
    s2 = okr.save_node("session", None, {"linked_krs": ["KR-01"]}, "ran y")
    assert s1["id"].endswith("-01") and s2["id"].endswith("-02")
    sess = okr.read_node("session", s1["id"])
    assert ("covers", s1["id"], "KR-01") in okr.edges_of(sess)
