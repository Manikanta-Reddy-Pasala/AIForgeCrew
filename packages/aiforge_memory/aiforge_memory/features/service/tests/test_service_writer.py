"""L2 — service_writer Cypher upsert + prune."""
from __future__ import annotations

import os

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.features.repo import store as repo_writer
from aiforge_memory.features.repo.extract import RepoSummary
from aiforge_memory.features.service import store as service_writer
from aiforge_memory.features.service.extract import ServiceDraft

pytestmark = pytest.mark.live_neo4j

REPO = "test_l2_repo"


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
    # ensure the parent Repo exists
    repo_writer.upsert_repo(
        drv, name=REPO, path="/tmp/" + REPO,
        summary=RepoSummary(runbook_md="r" * 600),
        pack_sha="sha-test",
    )
    yield drv
    with drv.session() as s:
        s.run(
            "MATCH (n) WHERE n.repo = $r OR n.name = $r DETACH DELETE n",
            r=REPO,
        ).consume()
    drv.close()


def test_upsert_creates_service_and_edges(driver) -> None:
    drafts = [
        ServiceDraft(
            name="api", role="api",
            files=["api/main.py", "api/routes.py"],
            tech_stack=["python", "fastapi"], port=8080,
            description="api svc", source="llm",
        ),
    ]
    counts = service_writer.upsert_services(driver, repo=REPO, services=drafts)
    assert counts["services"] == 1
    assert counts["file_edges"] == 2

    with driver.session() as s:
        n = s.run(
            "MATCH (r:Repo {name:$r})-[:OWNS_SERVICE]->(s:Service {name:'api'}) "
            "RETURN s.role AS role, s.port AS port", r=REPO,
        ).single()
        assert n["role"] == "api"
        assert n["port"] == 8080
        cnt = s.run(
            "MATCH (:Service {repo:$r, name:'api'})-[:CONTAINS_FILE]->(f) "
            "RETURN count(f) AS c", r=REPO,
        ).single()["c"]
        assert cnt == 2


def test_re_upsert_prunes_dropped_file_edges(driver) -> None:
    # First: 2 files
    service_writer.upsert_services(driver, repo=REPO, services=[
        ServiceDraft(name="api", role="api",
                     files=["api/main.py", "api/routes.py"], source="llm"),
    ])
    # Second: only 1 file remains
    service_writer.upsert_services(driver, repo=REPO, services=[
        ServiceDraft(name="api", role="api",
                     files=["api/main.py"], source="llm"),
    ])
    with driver.session() as s:
        cnt = s.run(
            "MATCH (:Service {repo:$r, name:'api'})-[:CONTAINS_FILE]->(f) "
            "RETURN count(f) AS c", r=REPO,
        ).single()["c"]
    assert cnt == 1


def test_re_upsert_prunes_dropped_services(driver) -> None:
    service_writer.upsert_services(driver, repo=REPO, services=[
        ServiceDraft(name="api", role="api", files=["api/main.py"], source="llm"),
        ServiceDraft(name="worker", role="consumer",
                     files=["worker/main.py"], source="llm"),
    ])
    # api dropped on re-ingest
    counts = service_writer.upsert_services(driver, repo=REPO, services=[
        ServiceDraft(name="worker", role="consumer",
                     files=["worker/main.py"], source="llm"),
    ])
    assert counts["pruned_services"] >= 1
    with driver.session() as s:
        n = s.run(
            "MATCH (s:Service {repo:$r}) RETURN s.name AS n", r=REPO,
        ).single()
    assert n["n"] == "worker"
