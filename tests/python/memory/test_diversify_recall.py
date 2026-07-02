"""Memory-recall correctness tests (adversarial-audit fixes).

Covers:
  #1 _diversify single-source cap skip + sqlite recall per-item groups
  #2 cross-source content dedup in query()
  #3 per-source min-max score normalization
  #4 partial-index status propagation in run_index
  #5 re-index over-count (deduped writes not counted)

No live Neo4j / Postgres / sidecars — everything offline.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def uq():
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    return uq


# ── Fix #1a: single-source cap skip ─────────────────────────────────

def test_diversify_skips_cap_single_source(uq, monkeypatch):
    """8 hits all collapsing to one group ("doer") → cap skipped, all kept."""
    monkeypatch.delenv("AIFORGE_DIVERSIFY_PER_GROUP", raising=False)
    hits = [{"source": "doer", "text": f"h{i}", "score": 8 - i}
            for i in range(8)]
    out = uq._diversify(hits, per_group=3)
    assert len(out) == 8
    assert [h["text"] for h in out] == [f"h{i}" for i in range(8)]


def test_diversify_single_source_via_group_key(uq):
    """Single distinct group by the `group` field (not source) → no cap."""
    hits = [{"source": "memory", "group": "doer", "text": str(i)}
            for i in range(6)]
    out = uq._diversify(hits, per_group=2)
    assert len(out) == 6


def test_diversify_multi_group_still_caps(uq):
    """>1 distinct group + per-group over cap → caps per group as before."""
    hits = [{"source": "memory", "ticket": "ONE-1", "text": str(i)}
            for i in range(5)]
    hits += [{"source": "doc", "text": "d1"},
             {"source": "doc", "text": "d2"},
             {"source": "doc", "text": "d3"},
             {"source": "doc", "text": "d4"}]
    out = uq._diversify(hits, per_group=3)
    # 3 from ONE-1 + 3 from doc (4th dropped) = 6
    assert len(out) == 6
    assert len([h for h in out if h.get("ticket") == "ONE-1"]) == 3
    assert len([h for h in out if h.get("source") == "doc"]) == 3


# ── Fix #1b: sqlite recall gives per-item groups ────────────────────

def test_sqlite_recall_rows_get_distinct_groups(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    sm.write_unit(text="alpha lambda cast fix for java", repo="demo")
    sm.write_unit(text="beta lambda cast fix for java too", repo="demo")
    rows = sm.recall("lambda cast fix java", limit=8, repo="demo")
    assert len(rows) >= 2
    groups = [r.get("group") for r in rows]
    assert all(g and g.startswith("sqlite:") for g in groups)
    assert len(set(groups)) == len(groups)  # distinct per row


# ── Fix #2: cross-source content dedup ──────────────────────────────

def test_dedup_identical_text_keeps_higher_score(uq):
    hits = [
        {"source": "doc", "text": "same body here", "score": 0.4},
        {"source": "afm_bundle", "text": "same body here", "score": 0.9},
        {"source": "memory", "text": "different body", "score": 0.5},
    ]
    out = uq._dedup(list(hits))
    texts = [h["text"] for h in out]
    assert texts.count("same body here") == 1
    assert "different body" in texts
    kept = [h for h in out if h["text"] == "same body here"][0]
    assert kept["score"] == 0.9  # the higher-scored survivor


def test_dedup_preserves_distinct_texts(uq):
    hits = [{"source": "a", "text": f"body {i}", "score": i} for i in range(5)]
    out = uq._dedup(list(hits))
    assert len(out) == 5


# ── Fix #3: per-source min-max normalization ────────────────────────

def test_normalize_relevance_hit_stays_competitive(uq):
    """A single fixed-score ticket (raw 1.0 w1.2) must not auto-bury a
    high-relevance memory hit (raw 0.9 in a 0.2-0.9 spread, w1.0)."""
    hits = []
    # ticket source: single fixed hit
    hits.append({"source": "ticket", "text": "T", "_raw_score": 1.0,
                 "_weight": 1.2, "score": 1.2})
    # memory source: spread, top raw 0.9
    spread = [0.2, 0.4, 0.6, 0.9]
    for i, raw in enumerate(spread):
        hits.append({"source": "memory", "text": f"M{i}", "_raw_score": raw,
                     "_weight": 1.0, "score": raw})
    out = uq._normalize_scores(hits)
    out.sort(key=lambda h: -h["score"])
    top_texts = [h["text"] for h in out[:2]]
    # the top memory hit (M3, raw 0.9 → normalized 1.0) must surface in top-2
    assert "M3" in top_texts


def test_normalize_single_hit_source_no_divzero(uq):
    hits = [{"source": "only", "text": "x", "_raw_score": 0.5,
             "_weight": 0.6, "score": 0.5}]
    out = uq._normalize_scores(hits)
    assert len(out) == 1
    assert out[0]["score"] == pytest.approx(0.6)  # span=0 → norm 1.0 × weight


def test_normalize_empty(uq):
    assert uq._normalize_scores([]) == []


# ── Fix #4: partial-index status propagation ────────────────────────

def test_run_index_reports_partial_on_layer_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "s.db"))
    import aiforge_core.runtime.memory_sources as ms
    importlib.reload(ms)
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)

    s = ms.create("repo", str(tmp_path), "r")
    mixed = {
        "units": 10, "code_units": 10, "doc_units": 0,
        "symbols": 0, "graphify_nodes": 0,
        "layers": {"code_chunks": "ok", "doc_chunks": "ok",
                   "symbols": "error:ts blew up", "graphify": "skip:no_neo4j"},
        "error": None,
    }
    monkeypatch.setattr(mi, "ingest_source", lambda src: mixed)

    calls = []
    orig = ms.set_status
    monkeypatch.setattr(ms, "set_status",
                        lambda *a, **k: (calls.append((a, k)), orig(*a, **k))[1])
    mi.run_index(s["id"])

    got = ms.get(s["id"])
    assert got["status"] == "partial"
    # the failing layer is surfaced somewhere operator-visible
    final = [c for c in calls if c[0][1] == "partial"]
    assert final, "set_status not called with 'partial'"
    _, kw = final[-1]
    assert kw.get("layers", {}).get("symbols", "").startswith("error:")


def test_run_index_done_when_all_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "s2.db"))
    import aiforge_core.runtime.memory_sources as ms
    importlib.reload(ms)
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    s = ms.create("repo", str(tmp_path), "r")
    ok = {"units": 5, "layers": {"code_chunks": "ok", "doc_chunks": "ok",
                                 "symbols": "skip:no_neo4j",
                                 "graphify": "skip:no_neo4j"},
          "error": None}
    monkeypatch.setattr(mi, "ingest_source", lambda src: ok)
    mi.run_index(s["id"])
    assert ms.get(s["id"])["status"] == "done"


# ── Fix #5: re-index over-count ─────────────────────────────────────

def test_write_deduped_not_counted(monkeypatch):
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    import aiforge_core.runtime.tools.memory_write as mw
    monkeypatch.setattr(mw, "memory_write",
                        lambda **kw: {"ok": True, "id": 0, "deduped": True})
    assert mi._write("t", kind="doc", repo="r", ref="f") is False


def test_write_neo4j_deduped_not_counted(monkeypatch):
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    import aiforge_core.runtime.tools.memory_write as mw
    monkeypatch.setattr(mw, "memory_write",
                        lambda **kw: {"ok": True, "id": "abc", "deduped": True})
    assert mi._write("t", kind="doc", repo="r", ref="f") is False


def test_write_real_counted(monkeypatch):
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    import aiforge_core.runtime.tools.memory_write as mw
    monkeypatch.setattr(mw, "memory_write",
                        lambda **kw: {"ok": True, "id": 7, "deduped": False})
    assert mi._write("t", kind="doc", repo="r", ref="f") is True
