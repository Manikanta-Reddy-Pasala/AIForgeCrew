"""L8 — schema gate: memory + doc indices/constraints exist after apply."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live_neo4j


@pytest.fixture(scope="module")
def driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        pytest.skip("neo4j driver not installed")
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            s.run("RETURN 1").consume()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    yield drv
    drv.close()


_REQUIRED_INDEX_NAMES = {
    "codemem_decision_unique",
    "codemem_decision_repo",
    "codemem_decision_status",
    "codemem_decision_ft",
    "codemem_observation_unique",
    "codemem_observation_repo",
    "codemem_observation_kind",
    "codemem_observation_ft",
    "codemem_observation_embed",
    "codemem_note_unique",
    "codemem_note_repo",
    "codemem_note_ft",
    "codemem_doc_unique",
    "codemem_doc_repo",
    "codemem_doc_ft",
    "codemem_file_test_flag",
    "codemem_file_lang",
    "codemem_symbol_visibility",
}


def test_apply_creates_memory_layer(driver) -> None:
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)
    with driver.session() as s:
        rows = list(s.run("SHOW INDEXES YIELD name"))
        names = {r["name"] for r in rows}
        # Constraints also show up under SHOW CONSTRAINTS
        crows = list(s.run("SHOW CONSTRAINTS YIELD name"))
        names |= {r["name"] for r in crows}
    missing = _REQUIRED_INDEX_NAMES - names
    assert not missing, f"missing schema entries: {missing}"
