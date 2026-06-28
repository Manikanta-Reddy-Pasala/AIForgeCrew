"""Gap #2 — observation supersession (contradiction resolution).

Unit-level: no live Neo4j. A recording fake driver captures the cypher
+ params each ``store`` call issues so we can assert the SUPERSEDES
wiring fires without a database.
"""
from __future__ import annotations

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


def test_upsert_observation_supersedes_marks_old_and_links():
    drv = _FakeDriver()
    store.upsert_observation(
        drv, repo="R", text="payee now uses accounting side",
        supersedes=["obs_old1"], dedupe=False,
    )
    supersede_calls = [
        (c, p) for c, p in drv.calls if "SUPERSEDES" in c
    ]
    assert supersede_calls, "expected a SUPERSEDES cypher to run"
    cypher, params = supersede_calls[0]
    assert "status = 'superseded'" in cypher
    # old id threaded as the superseded (b) node.
    assert "obs_old1" in str(params.values())


def test_upsert_observation_multiple_supersedes():
    drv = _FakeDriver()
    store.upsert_observation(
        drv, repo="R", text="new", supersedes=["a", "b"], dedupe=False,
    )
    supersede_calls = [c for c, _ in drv.calls if "SUPERSEDES" in c]
    assert len(supersede_calls) == 2


def test_upsert_observation_without_supersedes_emits_no_edge():
    drv = _FakeDriver()
    store.upsert_observation(drv, repo="R", text="lone fact", dedupe=False)
    assert not any("SUPERSEDES" in c for c, _ in drv.calls)


def test_recall_observation_query_excludes_superseded():
    assert "status" in store._RECALL_OBSERVATION
    assert "'active'" in store._RECALL_OBSERVATION
