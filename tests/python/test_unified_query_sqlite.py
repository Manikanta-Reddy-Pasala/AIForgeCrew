import importlib

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    # Force embedded mode + isolate the memory db; silence networked sources.
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_AFM_BUNDLE_ENABLED", "0")
    monkeypatch.setenv("AIFORGE_XREPO_ENABLED", "0")
    monkeypatch.setenv("AIFORGE_RERANK_DISABLE", "1")
    import aiforge_core.memory.backend_select as bs
    importlib.reload(bs)
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    return sm, uq


def test_embedded_recall_surfaces_in_unified_query(env, monkeypatch):
    sm, uq = env
    # Neutralize MCP sources so the test is deterministic/offline.
    monkeypatch.setattr(uq, "_mcp_call", lambda *a, **k: None, raising=False)
    sm.write_unit(text="the doer must cast lambda memory hints to fix Java",
                  kind="learning", source="learner", repo="demo")
    res = uq.query("how to fix the Java lambda cast issue", repo="demo", limit=5)
    assert "memory" in res["used_sources"]
    assert any("lambda" in (h.get("text") or "") for h in res["hits"])


# (Removed test_no_recall_when_not_embedded: NEO4J_URI no longer flips the
# memory backend — this is a SQLite-only build, so the embedded recall is
# always active.)
