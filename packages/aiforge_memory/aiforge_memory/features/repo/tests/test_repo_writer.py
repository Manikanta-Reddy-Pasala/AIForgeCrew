"""L1 — Repo node upsert via Cypher MERGE."""
from __future__ import annotations

import os

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.features.repo import store as repo_writer
from aiforge_memory.features.repo.extract import RepoSummary

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
    schema.apply(drv)
    yield drv
    # cleanup test repos
    with drv.session() as s:
        s.run("MATCH (r:Repo) WHERE r.name STARTS WITH 'test_' DETACH DELETE r").consume()
    drv.close()


def test_upsert_creates_node(driver) -> None:
    summary = RepoSummary(
        lang_primary="python",
        build_cmd="make build",
        test_cmd="make test",
        lint_cmd="ruff check",
        run_cmd="make run",
        portforward_cmds=["kubectl port-forward svc/x 8080:8080"],
        conventions_md="## Conv",
        runbook_md="## Runbook\n" + ("a" * 600),
    )
    repo_writer.upsert_repo(
        driver,
        name="test_codemem_repo_a",
        path="/tmp/test_codemem_repo_a",
        summary=summary,
        pack_sha="sha-A",
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n="test_codemem_repo_a"
        ).single()
    assert row is not None
    r = row["r"]
    assert r["build_cmd"] == "make build"
    assert r["lang_primary"] == "python"
    assert r["last_pack_sha"] == "sha-A"
    assert r["last_indexed_at"] is not None
    assert r["portforward_cmds"] == ["kubectl port-forward svc/x 8080:8080"]


def test_upsert_is_idempotent_and_updates_pack_sha(driver) -> None:
    summary = RepoSummary(lang_primary="python", build_cmd="x", runbook_md="r" * 600)
    repo_writer.upsert_repo(
        driver, name="test_codemem_repo_b", path="/tmp/b",
        summary=summary, pack_sha="sha-1",
    )
    repo_writer.upsert_repo(
        driver, name="test_codemem_repo_b", path="/tmp/b",
        summary=summary, pack_sha="sha-2",
    )
    with driver.session() as s:
        sha = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r.last_pack_sha AS sha",
            n="test_codemem_repo_b",
        ).single()["sha"]
    assert sha == "sha-2"
