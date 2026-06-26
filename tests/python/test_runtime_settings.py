"""Operator-tunable global LLM token knobs."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def rs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    # Drop any env overrides so we test store → default precedence cleanly.
    monkeypatch.delenv("AIFORGE_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AIFORGE_LOCAL_CTX_WINDOW", raising=False)
    import aiforge_core.config.runtime_settings as mod
    return importlib.reload(mod)


def test_defaults(rs):
    assert rs.get("max_output_tokens") == 32768
    assert rs.get("context_window") == 131072
    assert rs.all_settings() == {
        "max_output_tokens": 32768, "context_window": 131072}


def test_env_overrides_default(rs, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "16384")
    assert rs.get("max_output_tokens") == 16384


def test_store_wins_over_env(rs, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "16384")
    rs.set_many({"max_output_tokens": 65536})
    assert rs.get("max_output_tokens") == 65536


def test_set_persists_and_roundtrips(rs):
    rs.set_many({"max_output_tokens": 40000, "context_window": 200000})
    # fresh read off disk
    mod = importlib.reload(rs)
    assert mod.get("max_output_tokens") == 40000
    assert mod.get("context_window") == 200000


def test_rejects_out_of_bounds(rs):
    with pytest.raises(ValueError):
        rs.set_many({"max_output_tokens": 0})
    with pytest.raises(ValueError):
        rs.set_many({"context_window": 50})


def test_ignores_unknown_keys(rs):
    out = rs.set_many({"bogus": 5, "max_output_tokens": 50000})
    assert "bogus" not in out
    assert out["max_output_tokens"] == 50000


def test_escalating_llm_reads_output_cap(rs):
    rs.set_many({"max_output_tokens": 50000})
    from aiforge_core.runtime.escalating_llm import _build_one
    m = _build_one({"model_id": "openai/x",
                    "api_base": "https://e.example/v1", "api_key": "k"})
    assert m._additional_args.get("max_tokens") == 50000


def test_router_reads_context_window(rs):
    rs.set_many({"context_window": 222222})
    import aiforge_core.llm.router as router
    assert router._local_ctx_window("doer") == 222222
