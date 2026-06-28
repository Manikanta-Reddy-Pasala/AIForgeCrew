"""L2 — Service + File schema additions exist after schema.apply."""
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


def test_service_node_key_exists(driver) -> None:
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)
    with driver.session() as s:
        rows = list(s.run(
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
            "WHERE 'Service' IN labelsOrTypes "
            "  AND properties = ['repo', 'name']"
        ))
    assert len(rows) == 1


def test_file_node_key_exists(driver) -> None:
    from aiforge_memory.core import neo4j as schema

    schema.apply(driver)
    with driver.session() as s:
        rows = list(s.run(
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
            "WHERE 'File_v2' IN labelsOrTypes "
            "  AND properties = ['repo', 'path']"
        ))
    assert len(rows) == 1
