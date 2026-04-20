"""Store v2 tests. Uses ephemeral Postgres via AIFORGE_PGMEM_DSN env."""
from __future__ import annotations
import os
import pytest
import psycopg

DSN = os.environ.get("AIFORGE_PGMEM_DSN",
                     "host=127.0.0.1 port=5432 dbname=aiforge")


def _pg_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(),
                                 reason="aiforge DB not reachable; run install-pg-aiforge.sh")


@pytest.fixture
def store():
    from aiforge_core.store_v2 import Store
    s = Store(dsn=DSN)
    s.ensure_schema()
    # start clean
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("TRUNCATE memories RESTART IDENTITY")
        cur.execute("TRUNCATE memory_proposals RESTART IDENTITY")
    return s


def test_ensure_schema_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()  # no error


def test_append_t1_event(store, monkeypatch):
    # Stub embed so tests don't need live sidecar
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.001] * 1024)

    mid = store.append_event(
        parent_id="TICKET-77",
        kind="tool_call",
        title="search_code called",
        text="query: 'publishToRemoteServer' | hits: 3",
        metadata={"tool": "search_code", "top_k": 5},
        source="agent:developer",
    )
    assert mid > 0

    rows = store.get_episodic("TICKET-77")
    assert len(rows) == 1
    assert rows[0].kind == "tool_call"
    assert rows[0].metadata["tool"] == "search_code"
    assert rows[0].expires_at is None  # not set until ticket merges


def test_propose_semantic_queued(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.0] * 1024)
    pid = store.propose(
        tier="t2", wing="project", kind="fact",
        title="use WebFlux", text="Repo uses Spring WebFlux throughout.",
        source_trace="TICKET-77", proposed_by="fact_extract",
    )
    assert pid > 0
    pending = store.list_proposals(status="pending")
    assert len(pending) == 1

    # Approve → should insert into memories
    store.decide_proposal(pid, approve=True, decided_by="human")
    semantic = store.search_tier("t2", "WebFlux", top_k=5)
    assert len(semantic) == 1


def test_t4_upsert_chunk(store, monkeypatch):
    from aiforge_core import embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda t: [0.0] * 1024)
    mid = store.upsert_code_chunk(
        repo="aiforge",
        path="aiforge_core/store_v2.py",
        symbol="Store.append_event",
        text="def append_event(self, ...): ...",
        metadata={"lang": "python", "lines": "120-160"},
    )
    assert mid > 0
