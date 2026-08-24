"""OKR-DAG P2/P3 — in-memory graph traversal + surgical retrieval + compile."""
from __future__ import annotations

import tempfile

import pytest

from aiforge_core.memory import okf as okr


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    okr.save_node("objective", "O-01", {"title": "Stock engine",
                  "status": "active"}, "# Context\n\nBacktest momentum on Nifty.")
    okr.save_node("key_result", "KR-01", {"parent_objective": "O-01",
                  "title": "Backtest logic", "status": "in-progress",
                  "metrics": {"cagr": "15%"}}, "# Requirements\n- no bias")
    okr.save_node("learning", "L-01", {"scope": "global", "category": "devops"},
                  "Run testcontainers as non-root, never k8s.")
    okr.save_node("learning", "L-02", {"scope": ["O-01"]},
                  "Use survivorship-bias-free constituents.")
    okr.save_node("learning", "L-03", {"scope": ["O-99"]}, "unrelated rule")
    okr.save_node("session", "2026-07-10-01", {"linked_krs": ["KR-01"]}, "ran A")
    okr.save_node("session", "2026-07-10-02", {"linked_krs": ["KR-01"]}, "ran B")
    return okr.build(force=True)


def test_graph_traversal(graph):
    assert graph.objective_of("KR-01") == "O-01"
    assert graph.key_results("O-01") == ["KR-01"]
    assert graph.sessions_of("KR-01") == ["2026-07-10-02", "2026-07-10-01"]  # newest first
    learn = graph.learnings_for("O-01")
    assert "L-01" in learn
    assert "L-02" in learn
    assert "L-03" not in learn
    assert graph.counts()["learning"] == 3


def test_active_pointer_and_retrieve(graph):
    okr.set_active("KR-01")
    assert okr.get_active() == "KR-01"
    ctx = okr.retrieve(recent_sessions=2, graph=graph)
    assert ctx["objective"]["id"] == "O-01"
    assert "Backtest momentum" in ctx["objective"]["context"]
    assert ctx["active_kr"]["id"] == "KR-01"
    assert "no bias" in ctx["active_kr"]["body"]
    assert {l["id"] for l in ctx["learnings"]} == {"L-01", "L-02"}
    assert [s["id"] for s in ctx["sessions"]] == ["2026-07-10-02", "2026-07-10-01"]


def test_compile_prompt_shape(graph):
    block = okr.context_block("KR-01", graph=graph)
    assert "<OBJECTIVE id=\"O-01\">" in block
    assert "Stock engine" in block
    assert "<ACTIVE_TASK>" in block
    assert "KR-01 · Backtest logic [in-progress]" in block
    assert "<CRITICAL_RULES>" in block
    assert "non-root" in block
    assert "<RECENT_ACTIVITY>" in block
    assert "ran B" in block


def test_empty_when_no_active(graph):
    okr.set_active(None)
    assert okr.context_block(graph=graph) == ""
