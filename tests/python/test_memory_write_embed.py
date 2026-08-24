"""F5: memory_write's Neo4j branch must embed the observation text.

failure_memory / learner_persist embed before upsert so the node is
vector-recallable (and PPR-seedable). memory_write (which memory_ingest
chunks flow through) did NOT, so on Neo4j those chunks were invisible to
vector recall. These tests pin: embed succeeds -> embed_vec passed; embed
raises -> still writes (soft-fail, no vec, no crash).
"""
import pytest

from aiforge_core.runtime.tools import memory_write as mw


@pytest.fixture
def neo4j_backend(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: False,
    )

    class _FakeDriver:
        def close(self):
            pass

    class _FakeGDB:
        @staticmethod
        def driver(*a, **k):
            return _FakeDriver()

    monkeypatch.setattr("neo4j.GraphDatabase", _FakeGDB)
    captured = {}

    def _fake_upsert(drv, **kwargs):
        captured.update(kwargs)
        return {"id": "obs-1", "deduped": False}

    monkeypatch.setattr(
        "aiforge_memory.features.memory.store.upsert_observation", _fake_upsert,
    )
    return captured


def test_neo4j_write_passes_embed_vec(neo4j_backend, monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.embed.embed", lambda text: [0.1, 0.2, 0.3],
    )
    out = mw.memory_write("a chunk of ingested text", kind="note",
                          repo="demo")
    assert out["ok"] is True
    assert neo4j_backend.get("embed_vec") == [0.1, 0.2, 0.3]


def test_neo4j_write_soft_fails_when_embed_raises(neo4j_backend, monkeypatch):
    def _boom(text):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr("aiforge_core.memory.embed.embed", _boom)
    out = mw.memory_write("another chunk", kind="note", repo="demo")
    # writes anyway, just without a vector
    assert out["ok"] is True
    assert neo4j_backend.get("embed_vec") is None


def test_sqlite_path_uses_passed_source(monkeypatch):
    """F7: source must not be hardcoded 'doer' on the sqlite path so ingest
    chunks aren't mislabeled."""
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True,
    )
    captured = {}

    def _fake_write_unit(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(
        "aiforge_core.memory.sqlite_memory.write_unit", _fake_write_unit,
    )
    mw.memory_write("ingested chunk", kind="note", repo="demo",
                    source="ingest")
    assert captured.get("source") == "ingest"


def test_sqlite_path_source_defaults_doer(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True,
    )
    captured = {}
    monkeypatch.setattr(
        "aiforge_core.memory.sqlite_memory.write_unit",
        lambda **kw: captured.update(kw) or 1,
    )
    mw.memory_write("x", kind="note", repo="demo")
    assert captured.get("source") == "doer"


def test_write_tags_agent_role(monkeypatch):
    # every write is attributed: agent:<role> from the request context, else
    # agent:<source>.
    monkeypatch.setattr("aiforge_core.memory.backend_select.embedded", lambda: True)
    monkeypatch.setattr("aiforge_core.memory.md_store.capture", lambda *a, **k: {})
    cap = {}
    monkeypatch.setattr("aiforge_core.memory.sqlite_memory.write_unit",
                        lambda **kw: cap.update(kw) or 1)
    mw.memory_write("x", kind="note", repo="demo", source="doer")
    assert "agent:doer" in cap["tags"]
    from aiforge_core.runtime import request_context
    tok = request_context.set_role("planner")
    try:
        mw.memory_write("y", kind="note", repo="demo", source="doer")
    finally:
        request_context.reset_role(tok)
    assert "agent:planner" in cap["tags"]          # context role wins over source


def test_feed_brief_routes_through_okr_capture(monkeypatch):
    # a durable write feeds the OKR library (capture), tagged + not re-ingested.
    monkeypatch.setattr("aiforge_core.memory.backend_select.embedded", lambda: True)
    monkeypatch.setattr("aiforge_core.memory.sqlite_memory.write_unit", lambda **kw: 1)
    caps = []
    monkeypatch.setattr("aiforge_core.memory.md_store.capture",
                        lambda kind, text, **k: caps.append((kind, k)) or {})
    mw.memory_write("a durable fact", kind="gotcha", repo="demo", source="doer")
    assert caps
    assert caps[0][1].get("repo") == "demo"
    assert "agent:doer" in caps[0][1].get("tags", [])
    assert caps[0][1].get("ingest") is False        # backend already has it
