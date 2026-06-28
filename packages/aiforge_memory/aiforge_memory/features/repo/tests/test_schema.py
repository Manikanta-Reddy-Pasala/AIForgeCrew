"""L1 — Neo4j schema migration: Repo unique constraint + indices."""
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


def test_apply_ensures_repo_name_uniqueness(driver) -> None:
    """A uniqueness constraint on (:Repo {name}) must exist after apply,
    regardless of who owns the constraint name."""
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)

    with driver.session() as s:
        rows = list(s.run(
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
            "WHERE 'Repo' IN labelsOrTypes "
            "  AND properties = ['name'] "
            "  AND type IN ['UNIQUENESS', 'NODE_KEY']"
        ))
    assert len(rows) >= 1, "no uniqueness constraint on (:Repo).name"


def test_apply_creates_runbook_fulltext(driver) -> None:
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)
    with driver.session() as s:
        rows = list(s.run(
            "SHOW INDEXES YIELD name, type WHERE name = 'codemem_repo_runbook_ft'"
        ))
    assert len(rows) == 1
    assert rows[0]["type"] == "FULLTEXT"


def test_apply_is_idempotent(driver) -> None:
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)
    schema.apply(driver)  # second call must not raise
