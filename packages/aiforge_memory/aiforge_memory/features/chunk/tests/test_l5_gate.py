"""L5 gate — chunk + embed end-to-end on poly_repo fixture.

Mocks the bge-m3 sidecar (no live HTTP); hits real Neo4j to verify
Chunk_v2 nodes + CHUNKED_AS edges + vector index ready.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.core import state as sdb
from aiforge_memory.features.flow import runner as flow
from aiforge_memory.features.repo import extract as rs

HERE = Path(__file__).parent
POLY = HERE.parent.parent / "symbol" / "tests" / "fixtures" / "poly_repo"

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
    with drv.session() as s:
        s.run("MATCH (n) WHERE n.repo='l5_gate' OR n.name='l5_gate' "
              "DETACH DELETE n").consume()
    drv.close()


def test_l5_gate(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_vec = [0.01] * 1024

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L5")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=rs.RepoSummary(runbook_md="r" * 600)), \
         patch("aiforge_memory.features.flow.runner.service_extract.extract_services",
               return_value=[]), \
         patch("aiforge_memory.features.flow.runner.file_summary.summarize_files",
               return_value=[]), \
         patch("aiforge_memory.features.chunk.embed._embed",
               return_value=fake_vec):
        result = flow.ingest_repo(
            repo_name="l5_gate", repo_path=str(POLY),
            driver=driver, state_conn=state,
        )

    assert result.status == "indexed"
    assert result.chunks_count >= 5   # at least 1 chunk per file

    with driver.session() as s:
        cnt = s.run(
            "MATCH (c:Chunk_v2 {repo:'l5_gate'}) RETURN count(c) AS c"
        ).single()["c"]
        assert cnt >= 5

        chunked_edges = s.run(
            "MATCH (:File_v2 {repo:'l5_gate'})-[:CHUNKED_AS]->(:Chunk_v2) "
            "RETURN count(*) AS c"
        ).single()["c"]
        assert chunked_edges >= 5

        vidx = s.run(
            "SHOW INDEXES YIELD name WHERE name='codemem_chunk_embed'"
        ).single()
        assert vidx is not None


def test_l5_idempotent_re_ingest(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    fake_vec = [0.05] * 1024

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L5-IDEMP")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=rs.RepoSummary(runbook_md="r" * 600)), \
         patch("aiforge_memory.features.flow.runner.service_extract.extract_services",
               return_value=[]), \
         patch("aiforge_memory.features.flow.runner.file_summary.summarize_files",
               return_value=[]), \
         patch("aiforge_memory.features.chunk.embed._embed",
               return_value=fake_vec):
        first = flow.ingest_repo(
            repo_name="l5_gate", repo_path=str(POLY),
            driver=driver, state_conn=state,
        )
        second = flow.ingest_repo(
            repo_name="l5_gate", repo_path=str(POLY),
            driver=driver, state_conn=state,
        )
    assert first.status == "indexed"
    assert second.status == "skipped_unchanged"
