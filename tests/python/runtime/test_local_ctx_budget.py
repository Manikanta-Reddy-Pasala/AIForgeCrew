"""Local-first context/output budget sizing for small (32K) windows.

The defaults used to be sized for a 131K window and broke on a real 32K
local box (output cap reserved the WHOLE window; the condense budget didn't
fire until history was ~2x past a 32K overflow). These tests pin the
local-sane defaults + the window-aware budget/cave-auto behaviour.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def rs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AIFORGE_LOCAL_CTX_WINDOW", raising=False)
    import aiforge_core.config.runtime_settings as mod
    return importlib.reload(mod)


def test_local_first_defaults(rs):
    # A generation cap that fits ANY window; operators raise via env.
    assert rs.get("max_output_tokens") == 4096
    # The common local case; operators with 128K models set the env var.
    assert rs.get("context_window") == 131072


def test_big_model_operator_overrides_via_env(rs, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_MAX_TOKENS", "32768")
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")
    r = importlib.reload(rs)
    assert r.get("max_output_tokens") == 32768
    assert r.get("context_window") == 131072


# --- _ctx_budget_chars ------------------------------------------------------

@pytest.fixture
def ca(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_CAVE_AUTO_WINDOW", raising=False)
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    from aiforge_core.runtime import chat_agent as mod
    return mod


def test_budget_leaves_room_for_input_on_32k(ca, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")  # deterministic non-cave headroom
    budget = ca._ctx_budget_chars()
    win_chars = 32768 * 4
    # The output + system reservation was subtracted: budget must be below the
    # OLD naive `win*4*0.55` value (which reserved nothing for output/system).
    assert budget < int(win_chars * 0.55)
    assert budget < win_chars
    # Still a usable slice of history.
    assert budget >= 4000


def test_budget_never_below_floor_on_tiny_window(ca, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "4096")
    for cave in ("0", "1"):
        monkeypatch.setenv("AIFORGE_CAVE_MODE", cave)
        budget = ca._ctx_budget_chars()
        assert budget >= 4000       # never <=0 / below floor
        assert budget > 0


def test_cave_budget_smaller_than_non_cave(ca, monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    normal = ca._ctx_budget_chars()
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert ca._ctx_budget_chars() < normal


# --- window-aware cave auto-enable -----------------------------------------

def test_cave_auto_enables_on_small_window(ca, monkeypatch):
    monkeypatch.delenv("AIFORGE_CAVE_MODE", raising=False)
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")   # <= 48K
    assert ca._cave_mode() is True


def test_cave_off_on_large_window_when_unset(ca, monkeypatch):
    monkeypatch.delenv("AIFORGE_CAVE_MODE", raising=False)
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")  # big model
    assert ca._cave_mode() is False


def test_explicit_env_forces_cave_on_regardless_of_window(ca, monkeypatch):
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")
    assert ca._cave_mode() is True


def test_explicit_env_forces_cave_off_regardless_of_window(ca, monkeypatch):
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")   # small, but opted OUT
    assert ca._cave_mode() is False


def test_explicit_stored_zero_respected(ca, monkeypatch):
    monkeypatch.delenv("AIFORGE_CAVE_MODE", raising=False)
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")   # small window
    import aiforge_core.config.runtime_settings as rsmod
    rsmod.set_many({"cave_mode": 0})   # operator explicitly opted OUT via store
    assert ca._cave_mode() is False
