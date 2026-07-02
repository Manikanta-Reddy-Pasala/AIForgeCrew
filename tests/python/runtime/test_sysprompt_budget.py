"""Fix C2 + M1 — the (un-condensable) system prompt must be budgeted, and the
history budget must reserve the ACTUAL system-prompt size (not a fixed 14K).
"""
from __future__ import annotations

import importlib

import pytest

from aiforge_core.runtime import chat_agent as ca


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LOCAL_CTX_WINDOW", raising=False)
    monkeypatch.delenv("AIFORGE_SYS_PROMPT_FRAC", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.delenv("AIFORGE_LLM_MAX_TOKENS", raising=False)
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    yield
    importlib.reload(rsmod)


# ── _cap_system_prompt --------------------------------------------------------

def test_cap_brings_over_cap_prompt_under_budget():
    core = "CORE_PROMPT_MARKER: you are the assistant. FOLLOW THE RULES."
    sys_msg = core + "\n\n" + ("BLOAT " * 40000)   # huge injected tail
    budget = 5000
    out = ca._cap_system_prompt(sys_msg, budget, protect=len(core))
    assert len(out) <= budget                 # guaranteed under cap
    assert "CORE_PROMPT_MARKER" in out        # core (at the front) kept
    assert ca._SYS_CAP_MARK.strip() in out    # visible truncation marker


def test_cap_noop_when_under_budget():
    sys_msg = "small system prompt"
    assert ca._cap_system_prompt(sys_msg, 100000) is sys_msg
    assert ca._cap_system_prompt(sys_msg, 0) is sys_msg   # 0 disables


def test_sys_prompt_budget_scales_with_window(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    importlib.reload(importlib.import_module("aiforge_core.config.runtime_settings"))
    small = ca._sys_prompt_budget_chars()
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")
    importlib.reload(importlib.import_module("aiforge_core.config.runtime_settings"))
    assert ca._sys_prompt_budget_chars() > small


# ── M1: history budget reserves the ACTUAL system-prompt size -----------------

def test_ctx_budget_reserves_actual_sys_size(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")        # deterministic headroom
    importlib.reload(importlib.import_module("aiforge_core.config.runtime_settings"))
    small_sys = ca._ctx_budget_chars(sys_chars=1000)
    big_sys = ca._ctx_budget_chars(sys_chars=100000)
    # A bigger system prompt reserves more → leaves LESS for history.
    assert big_sys < small_sys
    # Backward compat: no sys_chars → falls back to the ~14K estimate.
    fallback = ca._ctx_budget_chars()
    assert fallback > 0
