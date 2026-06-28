"""N8 — observation dedupe by indexed text_hash, verified by full text."""
from __future__ import annotations

import hashlib

from aiforge_memory.core import neo4j as schema
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
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self._driver.calls.append((cypher, params))
        if "text_hash: $text_hash" in cypher and self._driver.dup_row:
            return _FakeResult([self._driver.dup_row])
        return _FakeResult()


class _FakeDriver:
    def __init__(self, dup_row=None):
        self.calls: list[tuple[str, dict]] = []
        self.dup_row = dup_row

    def session(self):
        return _FakeSession(self)


def test_dedupe_lookup_uses_hash_key_and_verifies_text():
    cy = store._FIND_DUP_OBSERVATION
    assert "text_hash: $text_hash" in cy
    assert "o.text = $text" in cy           # collision guard


def test_text_hash_is_short_sha256():
    text = "NATS retry loop polls every 30s"
    expected = hashlib.sha256(text.encode()).hexdigest()[:16]
    assert store._text_hash(text) == expected


def test_upsert_writes_text_hash():
    drv = _FakeDriver()
    store.upsert_observation(drv, repo="R", text="fact one", dedupe=False)
    upserts = [(c, p) for c, p in drv.calls if "MERGE (o:Observation_v2" in c]
    assert upserts
    cypher, params = upserts[0]
    assert "o.text_hash   = $text_hash" in cypher
    assert params["text_hash"] == store._text_hash("fact one")


def test_dedupe_hit_bumps_existing_node():
    drv = _FakeDriver(dup_row={"id": "obs_x", "seen_count": 2})
    res = store.upsert_observation(drv, repo="R", text="fact one")
    assert res["deduped"] is True
    assert res["id"] == "obs_x"
    assert res["seen_count"] == 3
    # Lookup carried the hash param.
    lookup = drv.calls[0]
    assert lookup[1]["text_hash"] == store._text_hash("fact one")
    # No new node created.
    assert not any("MERGE (o:Observation_v2" in c for c, _ in drv.calls)


def test_schema_has_text_hash_index():
    assert any(
        "codemem_observation_text_hash" in stmt
        and "(o.repo, o.text_hash)" in stmt
        for stmt in schema._INDEX_STATEMENTS
    )
