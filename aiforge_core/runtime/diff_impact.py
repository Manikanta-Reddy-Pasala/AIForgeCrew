"""Diff-aware test impact map (standards gap C12).

Given a set of changed file paths, walk the Neo4j File_v2 /
Symbol_v2 graph back up to test files. KISS: one Cypher with a
configurable hop count. Empty result → caller falls back to full
suite.

This pairs with the ``run_tests`` tool: pass ``pattern`` =
comma-joined output of ``impacted_tests(changed_paths)`` so ``pytest
-k`` / ``mvn -Dtest=`` only runs the relevant slice.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.diff_impact")

_IMPACTED_CYPHER = """
UNWIND $paths AS p
MATCH (changed:File_v2 {repo: $repo, path: p})
CALL {
    WITH changed
    MATCH (t:File_v2 {repo: $repo})-[:MENTIONS|CALLS|IMPORTS*1..$hops]->(changed)
    WHERE t.path =~ '(?i).*test.*'
    RETURN DISTINCT t.path AS test_path
}
RETURN collect(DISTINCT test_path) AS tests
"""


def _neo4j_driver_from_env():
    """Build a Neo4j driver from the standard env vars, or None on
    absence/failure (soft-fail)."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD",
                        os.environ.get("NEO4J_PASSWORD", "password"))
    try:
        return GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("diff_impact driver fail: %s", exc)
        return None


def _query_impacted(driver, repo: str, paths: list, hops: int) -> list[str]:
    """Run the impacted-tests Cypher; [] on any query error."""
    try:
        cy = _IMPACTED_CYPHER.replace("$hops", str(max(1, min(int(hops), 5))))
        with driver.session() as s:
            row = s.run(cy, repo=repo, paths=paths).single()
        return list((row or {}).get("tests", []) or [])
    except Exception as exc:  # noqa: BLE001
        log.debug("diff_impact query fail: %s", exc)
        return []


def impacted_tests(
    repo: str,
    changed_paths: list[str],
    *,
    hops: int = 3,
    driver=None,
) -> list[str]:
    """Return test-file paths likely impacted by ``changed_paths``.

    Soft-fail to empty list on any backend error — callers treat empty as "fall
    back to full suite".
    """
    paths = [p for p in (changed_paths or []) if isinstance(p, str) and p]
    if not paths or not repo:
        return []
    close_drv = driver is None
    if driver is None:
        driver = _neo4j_driver_from_env()
        if driver is None:
            return []
    try:
        return _query_impacted(driver, repo, paths, hops)
    finally:
        if close_drv:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
