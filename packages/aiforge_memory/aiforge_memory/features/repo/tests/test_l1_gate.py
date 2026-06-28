"""L1 gate — Stage 1+2 end-to-end on tiny_repo fixture.

This is the layer's contract test. It mocks the RepoMix subprocess
and the LLM so it can run without external dependencies — but it
hits a real (or skipped) Neo4j to verify the node is materialized.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.core import neo4j as schema
from aiforge_memory.core import state as sdb
from aiforge_memory.features.flow import runner as flow
from aiforge_memory.features.repo.extract import RepoSummary

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
EXPECTED = json.loads((HERE / "expected" / "tiny_repo_node.json").read_text())

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
        s.run("MATCH (r:Repo {name:'tiny_repo_test'}) DETACH DELETE r").consume()
    drv.close()


def _summary_from_fixture() -> RepoSummary:
    obj = json.loads((FIX / "llm_response_ok.json").read_text())
    return RepoSummary(
        lang_primary=obj["lang_primary"],
        build_cmd=obj["build_cmd"],
        test_cmd=obj["test_cmd"],
        lint_cmd=obj["lint_cmd"],
        run_cmd=obj["run_cmd"],
        portforward_cmds=obj["portforward_cmds"],
        conventions_md=obj["conventions_md"],
        runbook_md=obj["runbook_md"],
    )


def test_l1_gate(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    pack_text = (FIX / "tiny_pack.md").read_text()

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=(pack_text, "sha-L1GATE")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_summary_from_fixture()):
        result = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver,
            state_conn=state,
            skip_services=True,
        )
    assert result.status == "indexed"

    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:'tiny_repo_test'}) RETURN r"
        ).single()
    assert row is not None, "Repo node not created"
    r = row["r"]

    # 5/5 commands populated (lint may be empty by design)
    assert r["lang_primary"] == EXPECTED["lang_primary"]
    assert r["build_cmd"] == EXPECTED["build_cmd"]
    assert r["test_cmd"] == EXPECTED["test_cmd"]
    assert r["run_cmd"] == EXPECTED["run_cmd"]
    assert r["portforward_cmds"] == EXPECTED["portforward_cmds"]
    # Runbook contract (relaxed for tiny_repo fixture; real repos hit 500+)
    assert len(r["runbook_md"]) >= 200
    # Pack sha shape
    assert r["last_pack_sha"] == "sha-L1GATE"
    # Schema version stamp
    assert r["schema_version"] == EXPECTED["schema_version"]


def test_l1_gate_idempotent(driver, tmp_path) -> None:
    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    pack_text = (FIX / "tiny_pack.md").read_text()

    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=(pack_text, "sha-IDEMP")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=_summary_from_fixture()):
        first = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver, state_conn=state,
            skip_services=True,
        )
        second = flow.ingest_repo(
            repo_name="tiny_repo_test",
            repo_path=str(FIX / "tiny_repo"),
            driver=driver, state_conn=state,
            skip_services=True,
        )
    assert first.status == "indexed"
    assert second.status == "skipped_unchanged"
