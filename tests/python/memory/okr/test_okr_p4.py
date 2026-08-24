"""OKR-DAG P4 — auto-authoring nodes from a session (LLM stubbed)."""
from __future__ import annotations

import tempfile

import pytest

from aiforge_core.memory import okf as okr


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_OKR_AUTHOR", "1")


def _stub(monkeypatch, payload):
    from types import SimpleNamespace as NS

    def fake(role, messages, model, **k):
        # rebuild the pydantic-ish object from the payload, filling field defaults
        # (real pydantic models default the optional fields).
        def obj(d, defaults):
            return NS(**{**defaults, **d})
        return NS(
            objectives=[obj(o, {"title": "", "context": ""})
                        for o in payload.get("objectives", [])],
            key_results=[obj(k2, {"title": "", "objective_title": "", "metrics": ""})
                         for k2 in payload.get("key_results", [])],
            learnings=[obj(l, {"rule": "", "scope": "global"})
                       for l in payload.get("learnings", [])])
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", fake)


def test_extract_and_save_builds_graph(cfg, monkeypatch):
    _stub(monkeypatch, {
        "objectives": [{"title": "Stock engine", "context": "backtest momentum"}],
        "key_results": [{"title": "Backtest logic", "objective_title": "Stock engine",
                         "metrics": "cagr 15%"}],
        "learnings": [{"rule": "no k8s for tests", "scope": "global"},
                      {"rule": "survivorship-bias-free", "scope": "Stock engine"}],
    })
    r = okr.extract_and_save("a long enough session transcript about backtesting " * 3)
    assert r["ok"]
    assert r["objectives"]
    assert r["key_results"]
    g = okr.build(force=True)
    oid = r["objectives"][0]
    kid = r["key_results"][0]
    assert g.objective_of(kid) == oid                 # KR linked to objective
    learn = g.learnings_for(oid)
    assert len(learn) == 2                             # global + scoped both apply
    # retrieval over the just-authored graph
    okr.set_active(kid)
    block = okr.context_block()
    assert "Stock engine" in block
    assert "Backtest logic" in block
    assert "no k8s" in block


def test_extract_dedupes_objective_by_title(cfg, monkeypatch):
    okr.save_node("objective", "O-01", {"title": "Stock engine", "status": "active"}, "x")
    _stub(monkeypatch, {"objectives": [{"title": "stock ENGINE"}],  # same, diff case
                        "key_results": [{"title": "kr", "objective_title": "Stock engine"}],
                        "learnings": []})
    r = okr.extract_and_save("session text long enough to pass the length gate here")
    assert r["objectives"] == []                       # reused O-01, not a dup
    assert okr.build(force=True).objective_of(r["key_results"][0]) == "O-01"


def test_author_disabled(cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_AUTHOR", "0")
    assert okr.extract_and_save("x" * 100)["skipped"] == "disabled"


def test_migrate_from_briefs(cfg, monkeypatch):
    from aiforge_core.memory import md_store, okf as okr
    from aiforge_core.runtime import work_notes
    # two topic briefs (one split) + a non-knowledge file
    (md_store.brief_path("auth")).write_text(
        work_notes.render_note("knowledge", "auth", title="auth",
                               facts=["rotate keys 90d"]), encoding="utf-8")
    (md_store.brief_path("auth-2")).write_text(
        work_notes.render_note("knowledge", "auth-2", title="auth p2",
                               facts=["mTLS between services"]), encoding="utf-8")
    (md_store.brief_path("sync")).write_text(
        work_notes.render_note("knowledge", "sync", title="sync",
                               facts=["exponential backoff"]), encoding="utf-8")
    r = okr.migrate_from_briefs()
    assert r["ok"]
    assert r["migrated"] == 2
    g = okr.build(force=True)
    learns = [n for n in g.nodes.values() if n["type"] == "learning"]
    cats = {(n.get("meta") or {}).get("category") for n in learns}
    assert cats == {"auth", "sync"}
    auth = next(n for n in learns if (n.get("meta") or {}).get("category") == "auth")
    assert "rotate keys 90d" in auth["body"]
    assert "mTLS" in auth["body"]
    # idempotent
    assert okr.migrate_from_briefs()["migrated"] == 0
