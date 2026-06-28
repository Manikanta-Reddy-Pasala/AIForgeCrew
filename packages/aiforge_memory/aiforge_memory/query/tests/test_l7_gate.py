"""L7 gate — end-to-end query → ContextBundle.

Pre-populates a poly_repo ingest (mocked LLMs + embed sidecar), then
runs codemem.query.bundle.query() with a domain-phrase query and
verifies the bundle contains expected anchors and a non-empty render.
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
from aiforge_memory.features.repo import extract as rs
from aiforge_memory.features.service.extract import ServiceDraft
from aiforge_memory.query import bundle

HERE = Path(__file__).parent
POLY = HERE.parent.parent / "features" / "symbol" / "tests" / "fixtures" / "poly_repo"

pytestmark = pytest.mark.live_neo4j

REPO = "l7_gate"
_FAKE_VEC = [0.05] * 1024
_FILE_SUMMARY_RESPONSE = json.dumps({
    "summary": "Demo source file used by codemem fixtures.",
    "purpose_tags": ["fixture", "demo"],
})
_TRANSLATOR_RESPONSE = json.dumps({
    "intent": "fix",
    "services": ["api"],
    "files": ["api/main.py", "api/helpers.py"],
    "symbols": ["api/main.py::PaymentService::process"],
    "hops": 1,
    "keywords": ["payment", "process"],
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

    # Pre-populate full ingest
    state = sdb.open_db("/tmp/l7_state.db")
    sdb.migrate(state)
    services = [
        ServiceDraft(name="api", role="api", source="llm",
                     description="FastAPI HTTP service",
                     files=["api/main.py", "api/helpers.py"]),
        ServiceDraft(name="java_svc", role="library", source="llm",
                     files=["svc/Service.java"]),
    ]
    with patch("aiforge_memory.features.flow.runner.pack_repo.pack",
               return_value=("# pack", "sha-L7")), \
         patch("aiforge_memory.features.flow.runner.repo_summary.summarize",
               return_value=rs.RepoSummary(
                   runbook_md="## Build\n\n```\nmake build\n```\n" + ("r" * 600))), \
         patch("aiforge_memory.features.flow.runner.service_extract.extract_services",
               return_value=services), \
         patch("aiforge_memory.features.file.extract._call_llm",
               return_value=_FILE_SUMMARY_RESPONSE), \
         patch("aiforge_memory.features.chunk.embed._embed",
               return_value=_FAKE_VEC):
        flow.ingest_repo(
            repo_name=REPO, repo_path=str(POLY),
            driver=drv, state_conn=state, force=True,
        )

    yield drv
    with drv.session() as s:
        s.run("MATCH (n) WHERE n.repo=$r OR n.name=$r DETACH DELETE n",
              r=REPO).consume()
    drv.close()


def test_l7_bundle_with_translator_grounding(driver) -> None:
    """LLM grounding picks valid candidates → bundle hydrates and renders."""
    with patch("aiforge_memory.query.translator._embed_query",
               return_value=_FAKE_VEC), \
         patch("aiforge_memory.query.translator._call_llm",
               return_value=_TRANSLATOR_RESPONSE):
        b = bundle.query(
            "fix payment processing in api",
            repo=REPO, driver=driver,
        )
    assert b.intent == "fix"
    # Service hydrated
    assert any(s["name"] == "api" for s in b.services)
    # File hydrated with summary
    assert any(f["path"] == "api/main.py" for f in b.files)
    # Symbol hydrated
    assert any(s["fqname"] == "api/main.py::PaymentService::process"
               for s in b.symbols)
    # Render contains the markdown sections
    rendered = b.render()
    assert "## Anchor files" in rendered
    assert "## Symbols" in rendered
    assert "## Runbook" in rendered


def test_l7_fastpath_for_explicit_symbol(driver) -> None:
    """Query with `Class.method` triggers fastpath, hydrates symbol directly."""
    with patch("aiforge_memory.query.translator._embed_query",
               return_value=_FAKE_VEC), \
         patch("aiforge_memory.query.translator._call_llm",
               return_value=json.dumps({"intent":"investigate","services":[],"files":[],"symbols":[],"hops":1,"keywords":[]})):
        b = bundle.query(
            "trace PaymentService.process",
            repo=REPO, driver=driver,
        )
    assert b.fastpath_hit.startswith("symbol:")
    # Fastpath should have populated symbols by terminal name
    assert any("PaymentService::process" in s["fqname"] for s in b.symbols)


def test_l7_translator_hallucination_dropped(driver) -> None:
    """LLM names not in the catalog must be ignored, not propagated."""
    bad_response = json.dumps({
        "intent": "fix",
        "services": ["nonexistent-service"],
        "files": ["does/not/exist.py"],
        "symbols": ["fake::symbol"],
        "hops": 1, "keywords": [],
    })
    with patch("aiforge_memory.query.translator._embed_query",
               return_value=_FAKE_VEC), \
         patch("aiforge_memory.query.translator._call_llm",
               return_value=bad_response):
        b = bundle.query("anything", repo=REPO, driver=driver)
    # Hallucinated names should NOT appear
    assert not any(s["name"] == "nonexistent-service" for s in b.services)
    assert not any(f["path"] == "does/not/exist.py" for f in b.files)
    assert not any(s["fqname"] == "fake::symbol" for s in b.symbols)
