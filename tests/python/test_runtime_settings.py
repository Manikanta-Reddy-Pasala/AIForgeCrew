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


def test_defaults(rs, monkeypatch):
    # The suite turns the calls-per-minute ceiling OFF via env (see
    # tests/conftest.py) so a throttle does not park the whole run in a queue.
    # This test is about the BUILT-IN defaults, so drop that override.
    monkeypatch.delenv("AIFORGE_LLM_MAX_RPM", raising=False)
    # LOCAL-FIRST defaults: a generation cap that fits any window + a 32K
    # window (the common small-local case). Operators raise both via env.
    assert rs.get("max_output_tokens") == 8192
    assert rs.get("context_window") == 131072
    assert rs.get("vision_capable") == 0
    assert rs.get("cave_mode") == 1        # cave is the STANDARD DEFAULT now
    assert rs.all_settings() == {
        "max_output_tokens": 8192, "context_window": 131072,
        "vision_capable": 0, "cave_mode": 1, "compact_llm": 0,
        "ctx_no_recall": 0, "ctx_no_mentions": 0, "ctx_no_skills": 0,
        "ctx_no_workflows": 0, "ctx_no_repomap": 0, "ctx_no_summary": 0,
        # Per-turn chat budget guards: 2000 steps / 1h wall clock, and up to
        # 2 self-extensions while the turn is still producing new work.
        "chat_safety_cap": 2000, "chat_turn_deadline_s": 3600,
        "chat_cap_extensions": 2,
        "chat_unattended_cap": 2000,
        "llm_max_rpm": 5}


def test_stale_cave_zero_migrated_to_default(rs, monkeypatch):
    """A store seeded with the OLD cave_mode default (0) is cleared once so cave
    reverts to the new standard default (1). The migration is marked so it never
    re-runs."""
    import json
    p = rs._path()
    p.write_text(json.dumps({"cave_mode": 0, "context_window": 200000}))
    rs._migrate_stale_cave_default()
    assert rs.get("cave_mode") == 1                 # reverted to new default
    assert rs.get("context_window") == 200000       # other knobs untouched
    stored = rs._read_store()
    assert "cave_mode" not in stored                # stale value removed
    assert stored.get("_cave_default_v2") == 1      # marked


def test_deliberate_cave_off_on_fresh_install_preserved(rs):
    """A fresh install that saves cave_mode=0 is a REAL opt-out — set_many
    stamps the marker so the migration never clears it."""
    rs.set_many({"cave_mode": 0})                   # stamps _cave_default_v2
    assert rs._read_store().get("_cave_default_v2") == 1
    rs._migrate_stale_cave_default()                # must NOT clear it
    assert rs.get("cave_mode") == 0


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


def test_stored_ignores_env_and_defaults(rs, monkeypatch):
    """`stored` answers "did the operator save this in the UI", which is a
    different question from `explicit` — the chat turn deadline parses its own
    (fractional) env var and must not have it collapsed to an int on the way."""
    monkeypatch.setenv("AIFORGE_CHAT_TURN_DEADLINE_S", "1800.5")
    assert rs.stored("chat_turn_deadline_s") is None      # env is not "stored"
    assert rs.get("chat_turn_deadline_s") == 1800         # …but is readable
    rs.set_many({"chat_turn_deadline_s": 60})
    assert rs.stored("chat_turn_deadline_s") == 60


def test_unset_forgets_only_known_saved_knobs(rs):
    rs.set_many({"chat_safety_cap": 77, "context_window": 200000})
    out = rs.unset(["chat_safety_cap", "not_a_knob"])
    assert out["chat_safety_cap"] == 2000                 # back to the default
    assert out["context_window"] == 200000                # untouched
    assert "chat_safety_cap" not in rs._read_store()
    rs.unset(["chat_safety_cap"])                         # idempotent
