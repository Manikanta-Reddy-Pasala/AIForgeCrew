"""Embedded-mode write routing: learner/failure/doer writes hit SQLite."""
import importlib
from dataclasses import dataclass

import pytest


@pytest.fixture
def embedded(monkeypatch, tmp_path):
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    import aiforge_core.memory.backend_select as bs
    importlib.reload(bs)
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    import aiforge_core.runtime.learner_persist as lp
    importlib.reload(lp)
    import aiforge_core.runtime.failure_memory as fm
    importlib.reload(fm)
    return sm, lp, fm


@dataclass
class _Ticket:
    identifier: str = "ONE-100"
    project: str = "demo"
    title: str = "broken thing"


def test_learner_persist_writes_to_sqlite(embedded):
    sm, lp, _ = embedded
    out = lp.persist_facts(
        facts=[{"text": "always cast lambda memory hints in Java"},
               {"text": "DECISION: use SQLite for embedded memory"}],
        repo="demo", ticket_identifier="ONE-100",
    )
    assert out["written_observations"] == 1
    assert out["written_decisions"] == 1
    assert out["errors"] == []
    assert sm.stats()["total"] == 2
    hits = sm.recall("lambda java cast", repo="demo")
    assert any("lambda" in h["text"] for h in hits)


def test_failure_memory_writes_to_sqlite(embedded):
    sm, _, fm = embedded
    res = fm.record_failure(_Ticket(), verdict="fail", reason="compile error")
    assert res["ok"] is True
    s = sm.stats()
    assert s["by_kind"].get("failure") == 1
    hits = sm.recall("compile error failure", repo="demo")
    assert hits


def test_failure_memory_pass_is_noop(embedded):
    sm, _, fm = embedded
    res = fm.record_failure(_Ticket(), verdict="pass")
    assert res["ok"] is False
    assert sm.stats()["total"] == 0


def test_doer_memory_write_to_sqlite(embedded, monkeypatch):
    sm, _, _ = embedded
    monkeypatch.setenv("AIFORGE_REPO_ROOT", "/x/demo")
    import aiforge_core.runtime.tools.memory_write as mw
    importlib.reload(mw)
    res = mw.memory_write("the staging deploy needs the VPN on first", kind="gotcha")
    assert res["ok"] is True
    assert res["label"] == "Observation_v2"
    assert sm.stats()["total"] == 1


def test_learner_persist_dedupes(embedded):
    sm, lp, _ = embedded
    f = [{"text": "same learning twice"}]
    lp.persist_facts(facts=f, repo="demo", ticket_identifier="ONE-1")
    lp.persist_facts(facts=f, repo="demo", ticket_identifier="ONE-2")
    assert sm.stats()["total"] == 1
