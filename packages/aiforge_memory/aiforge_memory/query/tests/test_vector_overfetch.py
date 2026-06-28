"""K1 — query-side vector recall must over-fetch the global stage
(same rationale as features/memory/tests/test_vector_overfetch.py)."""
from __future__ import annotations

from aiforge_memory.query import bundle, translator


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


def test_translator_chunk_fulltext_overfetches():
    """Lucene ranks globally then we repo-filter — the fulltext anchor
    must over-fetch (k×4 cap 500) exactly like the old vector path."""
    from aiforge_memory.query import translator as tr

    captured = {}

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, cy, **params):
            captured.update(params)
            return []

    class _Drv:
        def session(self): return _Sess()

    tr._chunk_fulltext_topk(_Drv(), repo="r", text="find the widget", k=50)
    assert captured["k"] == 50
    assert captured["k_query"] == 200  # 50 × 4
def test_bundle_fallback_recall_overfetches():
    drv = _FakeDriver()
    bundle._vector_observations(drv, repo="R", query_vec=VEC, k=5)
    # First call is the PPR rerank (returns no rows on the fake), the
    # fallback recall follows — find the call carrying $k_query + $k.
    fallback = [
        (c, p) for c, p in drv.calls
        if "k_query" in p and "k" in p
    ]
    assert fallback, "fallback recall never ran"
    cypher, params = fallback[-1]
    assert "$k_query" in cypher
    assert "LIMIT $k" in cypher
    assert params["k_query"] > params["k"]
