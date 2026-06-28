"""L-live — domain/flow/tour/review extract+store against real Neo4j.

Builds a tiny fixture graph (Repo → Services → Files → Symbols → CALLS),
then exercises all three new features end-to-end + idempotency. Skips
when Neo4j is unreachable.
"""
from __future__ import annotations

import os

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.features.domain import extract as dx
from aiforge_memory.features.domain import store as dstore
from aiforge_memory.features.review import extract as rx
from aiforge_memory.features.tour import extract as tx
from aiforge_memory.features.tour import store as tstore

pytestmark = pytest.mark.live_neo4j

REPO = "test_domain_live_repo"

_SEED = """
MERGE (r:Repo {name:$repo})
WITH r
// two services
MERGE (api:Service {repo:$repo, name:'api'})  SET api.role='api'
MERGE (core:Service {repo:$repo, name:'core'}) SET core.role='core'
MERGE (r)-[:OWNS_SERVICE]->(api)
MERGE (r)-[:OWNS_SERVICE]->(core)
// files
MERGE (f1:File_v2 {repo:$repo, path:'api/ctrl.py'})
MERGE (f2:File_v2 {repo:$repo, path:'core/svc.py'})
MERGE (api)-[:CONTAINS_FILE]->(f1)
MERGE (core)-[:CONTAINS_FILE]->(f2)
// symbols
MERGE (s1:Symbol_v2 {repo:$repo, fqname:'api.Ctrl.handle'}) SET s1.kind='controller'
MERGE (s2:Symbol_v2 {repo:$repo, fqname:'core.Svc.do'})     SET s2.kind='method'
MERGE (f1)-[:DEFINES]->(s1)
MERGE (f2)-[:DEFINES]->(s2)
// cross-service call → api+core land in one domain; a 2-step flow
MERGE (s1)-[:CALLS]->(s2)
"""


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
    with drv.session() as s:
        s.run(_SEED, repo=REPO).consume()
    yield drv
    with drv.session() as s:
        s.run("MATCH (n) WHERE n.repo=$r OR n.name=$r DETACH DELETE n", r=REPO).consume()
    drv.close()


def _count(driver, cy, **kw):
    with driver.session() as s:
        return s.run(cy, **kw).single()[0]


def test_domains_extract_store_and_idempotent(driver):
    res = dx.extract_domains(driver, repo=REPO, naming=False)
    assert res.domains, "expected at least one domain"
    # api + core connected via CALLS → one domain covering both services
    assert any(set(d.services) == {"api", "core"} for d in res.domains)
    assert res.flows, "expected a flow from the controller entry"

    dstore.upsert_domains(driver, repo=REPO, domains=res.domains, flows=res.flows)
    n1 = _count(driver, "MATCH (d:Domain {repo:$r}) RETURN count(d)", r=REPO)
    f1 = _count(driver, "MATCH (fl:Flow {repo:$r}) RETURN count(fl)", r=REPO)
    assert n1 >= 1 and f1 >= 1
    # COVERS + STEP edges exist
    assert _count(driver, "MATCH (:Domain {repo:$r})-[c:COVERS]->() RETURN count(c)", r=REPO) >= 1
    assert _count(driver, "MATCH (:Flow {repo:$r})-[s:STEP]->() RETURN count(s)", r=REPO) >= 1

    # idempotent: re-run yields identical counts (no dup)
    res2 = dx.extract_domains(driver, repo=REPO, naming=False)
    dstore.upsert_domains(driver, repo=REPO, domains=res2.domains, flows=res2.flows)
    assert _count(driver, "MATCH (d:Domain {repo:$r}) RETURN count(d)", r=REPO) == n1
    assert _count(driver, "MATCH (fl:Flow {repo:$r}) RETURN count(fl)", r=REPO) == f1


def test_tour_build_and_store(driver):
    tour = tx.build_tour(driver, repo=REPO, naming=False)
    assert tour.stops, "expected tour stops"
    assert tour.stops[0]["node_id"] == "api.Ctrl.handle"  # entry first
    tstore.upsert_tour(driver, repo=REPO, tour=tour)
    assert _count(driver, "MATCH (t:Tour {repo:$r}) RETURN count(t)", r=REPO) == 1
    assert _count(driver, "MATCH (:Tour {repo:$r})-[s:STOP]->() RETURN count(s)", r=REPO) >= 2
    # idempotent re-run: still one tour, steps replaced not duplicated
    tour2 = tx.build_tour(driver, repo=REPO, naming=False)
    tstore.upsert_tour(driver, repo=REPO, tour=tour2)
    assert _count(driver, "MATCH (t:Tour {repo:$r}) RETURN count(t)", r=REPO) == 1


def test_review_finds_gaps(driver):
    report = rx.review_graph(driver, repo=REPO, naming=False)
    assert report.totals["services"] == 2
    assert report.totals["symbols"] == 2
    # files have no summary in the fixture → a low-severity finding
    kinds = {f.kind for f in report.findings}
    assert "files_no_summary" in kinds
