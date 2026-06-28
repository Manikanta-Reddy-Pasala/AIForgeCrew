"""K1 — repo-filtered vector recall must over-fetch the global stage.

Neo4j 5.x has no filtered vector search: queryNodes() ranks globally,
then our WHERE drops other repos' rows. Every recall path must therefore
send a separate ``$k_query > $k`` to the index call while keeping the
final ``LIMIT $k``. A recording fake driver captures the params.
"""
from __future__ import annotations

from aiforge_memory.core.neo4j import vector_overfetch_k
from aiforge_memory.features.memory import store


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
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._calls.append((cypher, params))
        return _FakeResult()


class _FakeDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def session(self):
        return _FakeSession(self.calls)


VEC = [0.1] * 1024


def test_overfetch_helper_scales_and_caps(monkeypatch):
    monkeypatch.delenv("AIFORGE_VECTOR_OVERFETCH", raising=False)
    assert vector_overfetch_k(1) == 10
    assert vector_overfetch_k(10) == 100
    assert vector_overfetch_k(100) == 500     # capped
    monkeypatch.setenv("AIFORGE_VECTOR_OVERFETCH", "3")
    assert vector_overfetch_k(5) == 15


def test_recall_observations_sends_k_query_above_k():
    drv = _FakeDriver()
    store.recall_observations(drv, repo="R", query_vec=VEC, k=10)
    cypher, params = drv.calls[0]
    assert "$k_query" in cypher
    assert "LIMIT $k" in cypher
    assert params["k_query"] > params["k"]


def test_find_semantic_dup_overfetches_the_k1_lookup():
    """The dead-dedupe path: k=1 with a global fetch of 1 almost never
    lands on the right repo. k_query must be > 1."""
    drv = _FakeDriver()
    store.find_semantic_dup(drv, repo="R", embed_vec=VEC)
    cypher, params = drv.calls[0]
    assert params["k"] == 1
    assert params["k_query"] > 1


def test_recall_observations_ppr_overfetches_seed_stage():
    drv = _FakeDriver()
    store.recall_observations_ppr(drv, repo="R", query_vec=VEC,
                                  k=10, seed_k=25)
    cypher, params = drv.calls[0]
    assert "$seed_k_query" in cypher
    assert params["seed_k_query"] > 25
    assert "LIMIT $k" in cypher
