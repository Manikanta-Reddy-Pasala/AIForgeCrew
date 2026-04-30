"""L3 gate — Stage 6 end-to-end on poly_repo fixture.

Mocks LLM to return deterministic summary; hits real Neo4j to check
that File_v2 nodes have summary + purpose_tags populated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_core.codemem.ingest import flow, repo_summary as rs
from aiforge_core.codemem.ingest.service_extract import ServiceDraft
from aiforge_core.codemem.store import schema, state_db as sdb


HERE = Path(__file__).parent
POLY = HERE.parent / "L4_symbols" / "fixtures" / "poly_repo"

pytestmark = pytest.mark.live_neo4j

_FIXED_SUMMARY = json.dumps({
    "summary": "Demo source file used by codemem fixtures.",
    "purpose_tags": ["fixture", "demo", "test"],
})


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
        s.run("MATCH (n) WHERE n.repo='l3_gate' OR n.name='l3_gate' "
              "DETACH DELETE n").consume()
    drv.close()


def test_l3_gate(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)

    with patch("aiforge_core.codemem.ingest.flow.pack_repo.pack",
               return_value=("# pack", "sha-L3")), \
         patch("aiforge_core.codemem.ingest.flow.repo_summary.summarize",
               return_value=rs.RepoSummary(runbook_md="r" * 600)), \
         patch("aiforge_core.codemem.ingest.flow.service_extract.extract_services",
               return_value=[]), \
         patch("aiforge_core.codemem.ingest.file_summary._call_llm",
               return_value=_FIXED_SUMMARY):
        result = flow.ingest_repo(
            repo_name="l3_gate", repo_path=str(POLY),
            driver=driver, state_conn=state,
        )

    assert result.status == "indexed"
    assert result.summaries_updated >= 4   # ≤ files_count, allowing for skips

    with driver.session() as s:
        rows = list(s.run(
            "MATCH (f:File_v2 {repo:'l3_gate'}) "
            "WHERE f.summary IS NOT NULL AND size(f.summary) > 0 "
            "RETURN count(f) AS c"
        ))
    assert rows[0]["c"] >= 4
