"""Store extensions — five additive gaps:

  M3 confidence, M1 recency/importance reranker, M2 semantic dedupe in
  core, M4 entity extraction, M8 soft-delete.

Unit-level only: Neo4j is offline. A recording fake driver captures the
cypher + params each ``store`` call issues (same pattern as
``test_supersede_unit.py``). Pure helpers (rerank, extract_entities) need
no driver at all.
"""
from __future__ import annotations

import math

from aiforge_memory.features.memory import store


# ─── fake driver (copied from test_supersede_unit.py) ─────────────────

class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, calls, results=None):
        self._calls = calls
        # ``results`` is an optional list of _FakeResult returned in order.
        self._results = results

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._calls.append((cypher, params))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


class _FakeDriver:
    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self._results = results

    def session(self):
        return _FakeSession(self.calls, self._results)


# ─── M3 confidence ────────────────────────────────────────────────────

def test_upsert_observation_passes_confidence_param():
    drv = _FakeDriver()
    store.upsert_observation(drv, repo="R", text="t", confidence=0.8,
                             dedupe=False)
    upsert = [(c, p) for c, p in drv.calls if "MERGE (o:Observation_v2" in c]
    assert upsert, "expected the observation upsert cypher to run"
    cypher, params = upsert[0]
    assert "o.confidence" in cypher
    assert params["confidence"] == 0.8


def test_upsert_observation_confidence_default_is_one():
    drv = _FakeDriver()
    store.upsert_observation(drv, repo="R", text="t", dedupe=False)
    _, params = next((c, p) for c, p in drv.calls
                     if "MERGE (o:Observation_v2" in c)
    assert params["confidence"] == 1.0


def test_upsert_observation_confidence_clamped():
    drv = _FakeDriver()
    store.upsert_observation(drv, repo="R", text="hi", confidence=5.0,
                             dedupe=False)
    drv2 = _FakeDriver()
    store.upsert_observation(drv2, repo="R", text="lo", confidence=-3.0,
                             dedupe=False)
    p_hi = next(p for c, p in drv.calls if "MERGE (o:Observation_v2" in c)
    p_lo = next(p for c, p in drv2.calls if "MERGE (o:Observation_v2" in c)
    assert p_hi["confidence"] == 1.0
    assert p_lo["confidence"] == 0.0


def test_upsert_decision_passes_confidence_param():
    drv = _FakeDriver()
    store.upsert_decision(drv, repo="R", title="T", confidence=0.5)
    upsert = [(c, p) for c, p in drv.calls if "MERGE (d:Decision_v2" in c]
    assert upsert
    cypher, params = upsert[0]
    assert "d.confidence" in cypher
    assert params["confidence"] == 0.5


# ─── M1 recency / importance reranker ─────────────────────────────────

def test_rerank_by_recency_pure_no_driver():
    now = 1_000_000.0
    day = 86_400.0
    rows = [
        {"id": "fresh", "score": 0.5,
         "created_at_epoch": now - 0.0, "confidence": 1.0},
        {"id": "stale", "score": 0.5,
         "created_at_epoch": now - 60 * day, "confidence": 1.0},
    ]
    out = store.rerank_by_recency(rows, now=now, half_life_days=30.0)
    # fresh gets near-full recency bonus, stale ~exp(-2).
    assert out[0]["id"] == "fresh"
    assert "final_score" in out[0]
    # check the math on the fresh row: 0.5 + 0.2*exp(0) + 0.1*0
    assert math.isclose(out[0]["final_score"], 0.5 + 0.2, rel_tol=1e-9)


def test_rerank_by_recency_confidence_weight():
    now = 1_000_000.0
    rows = [
        {"id": "low_conf", "score": 0.5,
         "created_at_epoch": now, "confidence": 0.0},
        {"id": "high_conf", "score": 0.5,
         "created_at_epoch": now, "confidence": 1.0},
    ]
    out = store.rerank_by_recency(rows, now=now, w_recency=0.0, w_conf=0.1)
    assert out[0]["id"] == "high_conf"
    # high_conf: 0.5 + 0 + 0.1*(1-1) = 0.5 ; low_conf: 0.5 + 0.1*(0-1) = 0.4
    assert math.isclose(out[0]["final_score"], 0.5, rel_tol=1e-9)
    assert math.isclose(out[1]["final_score"], 0.4, rel_tol=1e-9)


def test_rerank_by_recency_missing_fields_defaults():
    now = 1_000_000.0
    rows = [{"id": "bare", "score": 0.3}]  # no created_at_epoch, no confidence
    out = store.rerank_by_recency(rows, now=now)
    # missing created_at_epoch => no recency bonus; missing conf => 1.0 => 0 pen
    assert math.isclose(out[0]["final_score"], 0.3, rel_tol=1e-9)


def test_rerank_by_recency_empty():
    assert store.rerank_by_recency([], now=1.0) == []


# ─── M2 semantic dedupe in core ───────────────────────────────────────

def test_find_semantic_dup_returns_id_on_high_score():
    drv = _FakeDriver(results=[
        _FakeResult([{"id": "obs_x", "text": "t", "kind": "note",
                      "tags": [], "score": 0.95}]),
    ])
    out = store.find_semantic_dup(drv, repo="R", embed_vec=[0.1, 0.2],
                                  threshold=0.92)
    assert out == "obs_x"


def test_find_semantic_dup_returns_none_on_low_score():
    drv = _FakeDriver(results=[
        _FakeResult([{"id": "obs_x", "text": "t", "kind": "note",
                      "tags": [], "score": 0.5}]),
    ])
    out = store.find_semantic_dup(drv, repo="R", embed_vec=[0.1, 0.2],
                                  threshold=0.92)
    assert out is None


def test_find_semantic_dup_no_embed_returns_none():
    drv = _FakeDriver()
    assert store.find_semantic_dup(drv, repo="R", embed_vec=None) is None
    assert store.find_semantic_dup(drv, repo="R", embed_vec=[]) is None
    assert drv.calls == []  # never touched the driver


def test_find_semantic_dup_no_rows_returns_none():
    drv = _FakeDriver(results=[_FakeResult([])])
    assert store.find_semantic_dup(drv, repo="R", embed_vec=[0.1]) is None


def test_find_semantic_dup_uses_recall_observation_query():
    drv = _FakeDriver(results=[_FakeResult([])])
    store.find_semantic_dup(drv, repo="R", embed_vec=[0.1])
    cypher = drv.calls[0][0]
    assert "db.index.vector.queryNodes" in cypher


# ─── M4 entity extraction ─────────────────────────────────────────────

def test_extract_entities_file_paths():
    ents = store.extract_entities("see src/main/store.py and config.yaml")
    vals = {e["value"] for e in ents if e["type"] == "file"}
    assert "src/main/store.py" in vals
    assert "config.yaml" in vals


def test_extract_entities_symbols():
    ents = store.extract_entities("call MyClass::method and pkg.func")
    vals = {e["value"] for e in ents if e["type"] == "symbol"}
    assert "MyClass::method" in vals
    assert "pkg.func" in vals


def test_extract_entities_urls():
    ents = store.extract_entities("docs at https://example.com/x?y=1 ok")
    vals = {e["value"] for e in ents if e["type"] == "url"}
    assert "https://example.com/x?y=1" in vals


def test_extract_entities_env_vars():
    ents = store.extract_entities("set AIFORGE_REPO_ROOT and MAX_RETRIES")
    vals = {e["value"] for e in ents if e["type"] == "env"}
    assert "AIFORGE_REPO_ROOT" in vals
    assert "MAX_RETRIES" in vals


def test_extract_entities_ticket_ids():
    ents = store.extract_entities("fixes ONE-48 and ABC-1234 today")
    vals = {e["value"] for e in ents if e["type"] == "ticket"}
    assert "ONE-48" in vals
    assert "ABC-1234" in vals


def test_extract_entities_dedupes():
    ents = store.extract_entities("ONE-1 ONE-1 ONE-1")
    tickets = [e for e in ents if e["value"] == "ONE-1"]
    assert len(tickets) == 1


def test_extract_entities_empty():
    assert store.extract_entities("") == []
    assert store.extract_entities("just plain words here") == []


def test_upsert_observation_passes_entities_param():
    drv = _FakeDriver()
    store.upsert_observation(
        drv, repo="R",
        text="bug in src/store.py see ONE-7 set DEBUG_MODE",
        dedupe=False,
    )
    _, params = next((c, p) for c, p in drv.calls
                     if "MERGE (o:Observation_v2" in c)
    assert "o.entities" in next(c for c, _ in drv.calls
                                if "MERGE (o:Observation_v2" in c)
    ents = set(params["entities"])
    assert "src/store.py" in ents
    assert "ONE-7" in ents
    assert "DEBUG_MODE" in ents


# ─── M8 soft delete / restore ─────────────────────────────────────────

def test_soft_forget_sets_deleted_status():
    drv = _FakeDriver(results=[_FakeResult([{"id": "obs_1"}])])
    out = store.soft_forget(drv, repo="R", node_id="obs_1",
                            label="Observation_v2")
    cypher, params = drv.calls[0]
    assert "status = 'deleted'" in cypher or "status='deleted'" in cypher
    assert "deleted_at" in cypher
    assert "DETACH DELETE" not in cypher
    assert params["id"] == "obs_1"
    assert params["repo"] == "R"
    assert out["soft_deleted"] == "obs_1"


def test_soft_forget_rejects_bad_label():
    drv = _FakeDriver()
    try:
        store.soft_forget(drv, repo="R", node_id="x", label="Bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_restore_clears_status():
    drv = _FakeDriver(results=[_FakeResult([{"id": "obs_1"}])])
    out = store.restore(drv, repo="R", node_id="obs_1",
                        label="Observation_v2")
    cypher, params = drv.calls[0]
    assert "status = 'active'" in cypher or "status='active'" in cypher
    assert params["id"] == "obs_1"
    assert out["restored"] == "obs_1"


def test_restore_rejects_bad_label():
    drv = _FakeDriver()
    try:
        store.restore(drv, repo="R", node_id="x", label="Bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_recall_query_only_includes_active_or_null():
    # M8: vanilla recall must exclude both 'superseded' and 'deleted'.
    cy = store._RECALL_OBSERVATION
    assert "o.status IS NULL OR o.status = 'active'" in cy
