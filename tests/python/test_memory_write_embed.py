"""memory_write's SQLite write path.

The Neo4j branch these tests used to cover is GONE — ``_memory_write_impl``
writes SQLite unconditionally (single mode is SQLite; ``api.py`` and
``deploy/converge`` actively neutralise leftover NEO4J_* env). Its two tests
patched ``backend_select.embedded`` and ``aiforge_memory...upsert_observation``
and asserted on a capture dict the code no longer fills: one failed, and the
other passed only because ``{}.get("embed_vec")`` is also None. Removed rather
than left asserting nothing.
"""
from aiforge_core.runtime.tools import memory_write as mw


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
