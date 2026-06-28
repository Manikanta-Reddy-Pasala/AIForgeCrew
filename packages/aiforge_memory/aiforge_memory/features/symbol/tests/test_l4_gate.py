"""L4 gate — Stage 4+5 end-to-end on poly_repo fixture.

Hits real Neo4j. Mocks Stage 1+2+3 LLM calls so test is deterministic.
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
from aiforge_memory.features.service.extract import ServiceDraft

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "poly_repo"

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
        s.run(
            "MATCH (n) WHERE n.repo = 'l4_gate' OR n.name = 'l4_gate' "
            "DETACH DELETE n"
        ).consume()
    drv.close()


def _summary() -> rs.RepoSummary:
    return rs.RepoSummary(
        lang_primary="polyglot", build_cmd="make build",
        test_cmd="pytest", run_cmd="run",
        runbook_md="r" * 600,
    )


def _service_drafts() -> list[ServiceDraft]:
    return [
        ServiceDraft(name="api", role="api", source="llm",
                     files=["api/main.py", "api/helpers.py"]),
        ServiceDraft(name="java_svc", role="library", source="llm",
                     files=["svc/Service.java"]),
        ServiceDraft(name="web", role="ui", source="llm",
                     files=["web/main.ts", "web/helpers.ts"]),
    ]


def test_l4_gate(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L4")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_summary()), \
         patch("aiforge_memory.features.flow.runner.service_extract.extract_services",
               return_value=_service_drafts()):
        result = flow.ingest_repo(
            repo_name="l4_gate",
            repo_path=str(FIX),
            driver=driver, state_conn=state,
        )

    assert result.status == "indexed"
    assert result.files_count == 5
    # Symbol counts: classes(3) + methods(8) + functions(4)  ≈ 15
    assert result.symbols_count >= 12
    # Calls > 0 — at least bootstrap→Counter.increment + process→normalize
    assert result.calls_count >= 3

    with driver.session() as s:
        # Files
        f = s.run(
            "MATCH (f:File_v2 {repo:'l4_gate'}) RETURN count(f) AS c"
        ).single()["c"]
        assert f == 5

        # Symbols
        sym = s.run(
            "MATCH (s:Symbol_v2 {repo:'l4_gate'}) RETURN count(s) AS c"
        ).single()["c"]
        assert sym >= 12

        # DEFINES edges = symbol count (every symbol defined by exactly 1 file)
        defs = s.run(
            "MATCH (:File_v2 {repo:'l4_gate'})-[:DEFINES]->(:Symbol_v2) "
            "RETURN count(*) AS c"
        ).single()["c"]
        assert defs == sym

        # CALLS edges (some, with confidence)
        calls = s.run(
            "MATCH (:Symbol_v2 {repo:'l4_gate'})-[c:CALLS]->(:Symbol_v2) "
            "RETURN count(c) AS c"
        ).single()["c"]
        assert calls >= 3

        # Specific edges expected
        proc_to_norm = s.run(
            "MATCH (a:Symbol_v2 {repo:'l4_gate', "
            "  fqname:'api/main.py::PaymentService::process'}) "
            "-[:CALLS]->(b:Symbol_v2 {fqname:'api/helpers.py::normalize'}) "
            "RETURN count(*) AS c"
        ).single()["c"]
        assert proc_to_norm >= 1


def test_l4_idempotent_re_ingest(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L4-IDEMP")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_summary()), \
         patch("aiforge_memory.features.flow.runner.service_extract.extract_services",
               return_value=_service_drafts()):
        first = flow.ingest_repo(
            repo_name="l4_gate", repo_path=str(FIX),
            driver=driver, state_conn=state,
        )
        second = flow.ingest_repo(
            repo_name="l4_gate", repo_path=str(FIX),
            driver=driver, state_conn=state,
        )
    assert first.status == "indexed"
    assert second.status == "skipped_unchanged"
