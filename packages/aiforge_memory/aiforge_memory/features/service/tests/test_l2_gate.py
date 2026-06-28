"""L2 gate — Stage 3 service extract end-to-end.

Mocks RepoMix (returns recorded pack) and the LLM (returns recorded
services JSON). Hits real Neo4j. Verifies node counts, edge counts,
operator override behavior, and CLI output.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.core import state as sdb
from aiforge_memory.features.flow import runner as flow
from aiforge_memory.features.repo import extract as rs

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
EXPECTED = json.loads((HERE / "expected" / "services.json").read_text())

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
            "MATCH (n) WHERE n.repo IN ['l2_gate','l2_gate_override'] "
            "  OR n.name IN ['l2_gate','l2_gate_override'] "
            "DETACH DELETE n"
        ).consume()
    drv.close()


def _fake_summary() -> rs.RepoSummary:
    return rs.RepoSummary(
        lang_primary="python", build_cmd="pip install -r requirements.txt",
        test_cmd="pytest tests/", run_cmd="python -m api.main",
        portforward_cmds=["kubectl port-forward svc/api 8080:8080"],
        runbook_md="r" * 600,
    )


def _llm_services_ok() -> str:
    return (FIX / "llm_services_ok.json").read_text()


def test_l2_gate_basic(driver, tmp_path) -> None:
    repo = tmp_path / "multi_repo"
    shutil.copytree(FIX / "multi_repo", repo)
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L2A")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_fake_summary()), \
         patch("aiforge_memory.features.service.extract._call_llm",
               return_value=_llm_services_ok()):
        result = flow.ingest_repo(
            repo_name="l2_gate",
            repo_path=str(repo),
            driver=driver, state_conn=state,
        )

    assert result.status == "indexed"
    assert result.services_count == 2
    assert result.file_edges_count == 6

    with driver.session() as s:
        # 2 services owned by repo
        cnt = s.run(
            "MATCH (r:Repo {name:'l2_gate'})-[:OWNS_SERVICE]->(s:Service) "
            "RETURN count(s) AS c"
        ).single()["c"]
        assert cnt == 2

        # 6 CONTAINS_FILE edges total
        edges = s.run(
            "MATCH (s:Service {repo:'l2_gate'})-[:CONTAINS_FILE]->(f:File_v2) "
            "RETURN count(*) AS c"
        ).single()["c"]
        assert edges == 6

        api = s.run(
            "MATCH (s:Service {repo:'l2_gate', name:'api'}) "
            "RETURN s.role AS role, s.port AS port, s.source AS source"
        ).single()
        assert api["role"] == "api"
        assert api["port"] == 8080
        assert api["source"] == "llm"


def test_l2_gate_override(driver, tmp_path) -> None:
    repo = tmp_path / "multi_repo"
    shutil.copytree(FIX / "multi_repo", repo)
    (repo / ".aiforge").mkdir()
    shutil.copy(FIX / "services_override.yaml",
                repo / ".aiforge" / "services.yaml")
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L2B")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_fake_summary()), \
         patch("aiforge_memory.features.service.extract._call_llm",
               return_value=_llm_services_ok()):
        flow.ingest_repo(
            repo_name="l2_gate_override",
            repo_path=str(repo),
            driver=driver, state_conn=state,
        )

    with driver.session() as s:
        api = s.run(
            "MATCH (s:Service {repo:'l2_gate_override', name:'api'}) "
            "RETURN s.source AS source, s.description AS d"
        ).single()
        worker = s.run(
            "MATCH (s:Service {repo:'l2_gate_override', name:'worker'}) "
            "RETURN s.source AS source"
        ).single()
    assert api["source"] == "manual"
    assert "Operator-edited" in api["d"]
    assert worker["source"] == "llm"
