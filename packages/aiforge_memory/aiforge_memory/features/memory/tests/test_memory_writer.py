"""L8 — memory_writer Decision/Observation/Note/Doc + MENTIONS edges.

Live-Neo4j test. Cleanup at the end of the module.
"""
from __future__ import annotations

import os

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.features.memory import store as memory_writer
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

    # Bootstrap a Repo node these memories can attach to.
    repo_writer.upsert_repo(
        drv, name="test_mem_repo",
        path="/tmp/test_mem_repo",
        summary=RepoSummary(lang_primary="python", runbook_md="r" * 200),
        pack_sha="sha",
    )
    # Plus a File_v2 + Symbol_v2 to test MENTIONS edges.
    with drv.session() as s:
        s.run(
            "MERGE (f:File_v2 {repo:'test_mem_repo', path:'src/foo.py'}) "
            "ON CREATE SET f.schema_version='codemem-v1' "
            "MERGE (sy:Symbol_v2 {repo:'test_mem_repo', "
            "       fqname:'foo.bar.Baz::run'}) "
            "ON CREATE SET sy.schema_version='codemem-v1'"
        ).consume()

    yield drv

    with drv.session() as s:
        s.run(
            "MATCH (n) WHERE n.repo = 'test_mem_repo' DETACH DELETE n"
        ).consume()
        s.run("MATCH (r:Repo {name:'test_mem_repo'}) DETACH DELETE r").consume()
    drv.close()


def test_decision_creates_node_with_records_edge(driver) -> None:
    out = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Use NATS over Kafka",
        body="ADR-001",
        rationale="Lower ops overhead in single-cluster deployment.",
        author="manik", session_id="s1",
        tags=["arch", "messaging"],
        refs=["src/foo.py"],
    )
    assert out["label"] == "Decision_v2"
    nid = out["id"]
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:'test_mem_repo'})-[:RECORDS]->"
            "(d:Decision_v2 {id:$id})-[:MENTIONS]->(f:File_v2) "
            "RETURN d.title AS title, d.rationale AS r, f.path AS p",
            id=nid,
        ).single()
    assert row["title"] == "Use NATS over Kafka"
    assert "Lower ops" in row["r"]
    assert row["p"] == "src/foo.py"


def test_decision_supersedes_chains_status(driver) -> None:
    a = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Original", body="b1", status="active",
    )
    b = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Revised", body="b2",
        supersedes_id=a["id"],
    )
    with driver.session() as s:
        old = s.run(
            "MATCH (d:Decision_v2 {id:$id}) RETURN d.status AS st",
            id=a["id"],
        ).single()
        edge = s.run(
            "MATCH (n:Decision_v2 {id:$nid})-[:SUPERSEDES]->"
            "(o:Decision_v2 {id:$oid}) RETURN o.id AS oid",
            nid=b["id"], oid=a["id"],
        ).single()
    assert old["st"] == "superseded"
    assert edge is not None


def test_observation_links_symbol_via_mentions(driver) -> None:
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        kind="bug",
        text="Race condition under concurrent push",
        refs=["foo.bar.Baz::run"],
        embed_vec=None,
    )
    assert out["label"] == "Observation_v2"
    with driver.session() as s:
        row = s.run(
            "MATCH (o:Observation_v2 {id:$id})-[:MENTIONS]->(s:Symbol_v2) "
            "RETURN o.kind AS k, s.fqname AS fq",
            id=out["id"],
        ).single()
    assert row["k"] == "bug"
    assert row["fq"] == "foo.bar.Baz::run"


def test_observation_dedupe_collapses_same_text(driver) -> None:
    """Same repo + same text → return the existing node id with
    deduped=True and seen_count bumped. The pre-dedupe implementation
    created a fresh Observation_v2 every Learner fire, drowning
    queries in restated facts."""
    text = "Pipeline restart wipes ipython kernel sessions."
    a = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text=text, kind="gotcha",
        tags=["runtime"],
    )
    assert a["deduped"] is False

    b = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text=text, kind="gotcha",
        tags=["runtime", "extra"],
    )
    assert b["deduped"] is True
    assert b["id"] == a["id"]
    assert b["seen_count"] >= 2

    # Different text → fresh node, not deduped.
    c = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text=text + " v2", kind="gotcha",
    )
    assert c["deduped"] is False
    assert c["id"] != a["id"]


def test_observation_dedupe_can_be_disabled(driver) -> None:
    text = "Force-create branch for the test path."
    a = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text=text,
    )
    b = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text=text, dedupe=False,
    )
    assert a["id"] != b["id"]
    assert b["deduped"] is False


def test_observation_records_media_refs_and_event_time(driver) -> None:
    """Gap-10 + gap-7: ``media_refs`` round-trips and ``event_time``
    is stored separately from ``created_at`` so bi-temporal queries
    can distinguish "when the fact refers to" from "when we ingested
    it"."""
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        text="screenshot from an old user report",
        kind="bug",
        media_refs=[
            "/var/uploads/2025-01-01/screen-001.png",
            "/var/uploads/2025-01-01/screen-002.png",
        ],
        event_time=1735689600.0,  # 2025-01-01 00:00 UTC
    )
    assert out["label"] == "Observation_v2"
    with driver.session() as s:
        row = s.run(
            "MATCH (o:Observation_v2 {id:$id}) "
            "RETURN o.media_refs AS media, o.event_time AS et, "
            "       o.created_at AS ct",
            id=out["id"],
        ).single()
    assert list(row["media"]) == [
        "/var/uploads/2025-01-01/screen-001.png",
        "/var/uploads/2025-01-01/screen-002.png",
    ]
    assert row["et"] is not None
    # event_time should be the 2025 date we asked for, not the ingest
    # moment (which is "now").
    assert str(row["et"]).startswith("2025-")
    assert row["ct"] is not None
    assert row["et"] != row["ct"]


def test_observation_event_time_defaults_to_now(driver) -> None:
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        text="fact with no explicit event_time",
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (o:Observation_v2 {id:$id}) "
            "RETURN o.event_time AS et, o.created_at AS ct",
            id=out["id"],
        ).single()
    # When caller doesn't supply event_time we fall back to ingest
    # timestamp so old callers stay correct without a migration.
    assert row["et"] is not None
    assert str(row["et"])[:4] == str(row["ct"])[:4]


def test_recall_observations_ppr_blends_vector_and_overlap(driver) -> None:
    """Gap-6: PPR-lite reranker. Vector recall finds the seed
    Observation; the rerank then surfaces a *different* Observation
    that shares a :MENTIONS neighbour with the seed."""
    base_vec = [0.0] * 1024
    base_vec[0] = 1.0
    # Two observations mention the same Symbol_v2:
    #   seed_obs has high vec sim to the query
    #   peer_obs has low vec sim but shares a symbol neighbour
    seed = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        text="ppr_seed: race condition in foo.bar.Baz::run",
        refs=["foo.bar.Baz::run"],
        embed_vec=base_vec,
    )
    distant_vec = [0.0] * 1024
    distant_vec[10] = 1.0
    peer = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        text="ppr_peer: also touches foo.bar.Baz::run from another angle",
        refs=["foo.bar.Baz::run"],
        embed_vec=distant_vec,
    )
    rows = memory_writer.recall_observations_ppr(
        driver, repo="test_mem_repo",
        query_vec=base_vec, k=10, seed_k=10, alpha=0.5,
    )
    ids = [r["id"] for r in rows]
    # The seed itself must rank highly because it has direct vec sim.
    assert seed["id"] in ids
    # The peer must surface despite low vec sim because it shares a
    # symbol neighbour — pure vector recall would miss it at
    # ``alpha=0.5``.
    assert peer["id"] in ids
    seed_row = next(r for r in rows if r["id"] == seed["id"])
    peer_row = next(r for r in rows if r["id"] == peer["id"])
    # Each row exposes the component scores for debuggability.
    assert "vec_score" in seed_row
    assert "overlap_score" in peer_row


def test_observation_with_vector_index(driver) -> None:
    """Observation_v2 with embed_vec should be retrievable via the
    vector index. Uses a stub 1024d embedding."""
    vec = [0.0] * 1024
    vec[0] = 1.0
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text="learned that JetStream needs ack",
        kind="learning", embed_vec=vec,
    )
    rows = memory_writer.recall_observations(
        driver, repo="test_mem_repo", query_vec=vec, k=5,
    )
    ids = [r["id"] for r in rows]
    assert out["id"] in ids


def test_note_basic(driver) -> None:
    out = memory_writer.upsert_note(
        driver, repo="test_mem_repo",
        title="Onboarding", body="set up venv first", tags=["howto"],
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (n:Note_v2 {id:$id}) RETURN n.title AS t, n.body AS b",
            id=out["id"],
        ).single()
    assert row["t"] == "Onboarding"
    assert "venv" in row["b"]


def test_doc_basic(driver) -> None:
    out = memory_writer.upsert_doc(
        driver, repo="test_mem_repo",
        title="NATS docs", body="JetStream consumers...",
        url="https://docs.nats.io/jetstream", source_kind="web",
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (d:Doc_v2 {id:$id}) RETURN d.url AS u, d.source_kind AS k",
            id=out["id"],
        ).single()
    assert row["u"].startswith("https://docs.nats.io")
    assert row["k"] == "web"


def test_list_memory_returns_all_kinds(driver) -> None:
    rows = memory_writer.list_memory(driver, repo="test_mem_repo", limit=200)
    labels = {r["label"] for r in rows}
    assert "Decision_v2" in labels
    assert "Observation_v2" in labels
    assert "Note_v2" in labels


def test_forget_removes_node(driver) -> None:
    n = memory_writer.upsert_note(
        driver, repo="test_mem_repo", title="ephemeral", body="x",
    )
    res = memory_writer.forget(
        driver, repo="test_mem_repo", node_id=n["id"], label="Note_v2",
    )
    assert res["deleted"] == n["id"]
    with driver.session() as s:
        row = s.run(
            "MATCH (n:Note_v2 {id:$id}) RETURN n", id=n["id"],
        ).single()
    assert row is None


def test_forget_unknown_label_raises(driver) -> None:
    with pytest.raises(ValueError):
        memory_writer.forget(
            driver, repo="test_mem_repo", node_id="x", label="Bogus_v2",
        )
