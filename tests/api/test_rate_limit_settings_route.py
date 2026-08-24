"""The rate-limit knobs over HTTP: saveable, and the runtime reads what saved.

Two layers that store-level tests never see, both of which have burned this
codebase before:

1. The route's pydantic body is the ONLY write path. A bound there that does
   not match ``_BOUNDS`` makes the store's own bound unreachable — the UI
   offers a value and gets a 422 for it, and nothing in the body is written,
   not even the sibling field.
2. A knob read from ``os.environ`` alone is INERT no matter what the UI saves.
   The store is what the UI writes, so the runtime has to resolve
   stored -> env -> default like every other knob.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_LLM_RATE_LIMIT_BACKOFF_S", "AIFORGE_LLM_RATE_LIMIT_CAP_S",
              "AIFORGE_LLM_MAX_RPM"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_the_knobs_save_and_the_limiter_reads_what_was_saved(client):
    r = client.put("/api/runtime/llm-settings",
                   json={"llm_rate_limit_backoff_s": 35,
                         "llm_rate_limit_cap_s": 90,
                         "llm_max_rpm": 15})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["llm_rate_limit_backoff_s"] == 35
    assert body["llm_rate_limit_cap_s"] == 90
    assert body["llm_max_rpm"] == 15

    # …and the runtime resolves the STORED value, not just the env var.
    from aiforge_core.llm import rate_limiter as rl
    assert rl.global_rpm() == 15
    assert rl._hold_cap() == 90.0
    assert rl._setting("llm_rate_limit_backoff_s",
                       "AIFORGE_LLM_RATE_LIMIT_BACKOFF_S", 20.0) == 35.0


def test_the_saved_cap_actually_bounds_a_hostile_retry_after(client):
    """End to end: the number in the Settings box is what bounds how long one
    server response can park the process."""
    from aiforge_core.llm import rate_limiter as rl
    rl.reset_global()
    assert client.put("/api/runtime/llm-settings",
                      json={"llm_rate_limit_cap_s": 10}).status_code == 200
    rl.note_rate_limited(3600.0)
    assert rl.held_for() <= 10.01
    rl.reset_global()


def test_zero_backoff_is_saveable(client):
    """0 means "use the ordinary exponential backoff" — a real choice, so the
    route must accept it rather than 422 the way the old chat-cap body did."""
    r = client.put("/api/runtime/llm-settings",
                   json={"llm_rate_limit_backoff_s": 0})
    assert r.status_code == 200, r.text
    assert r.json()["llm_rate_limit_backoff_s"] == 0


def test_a_zero_cap_is_refused(client):
    """A cap of 0 would mean "obey no rejection at all", which is not
    something an operator can usefully ask for: llm_max_rpm=0 already says
    "do not throttle me", and even that keeps obeying the server."""
    assert client.put("/api/runtime/llm-settings",
                      json={"llm_rate_limit_cap_s": 0}).status_code == 422
