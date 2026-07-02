"""Fix A1 — the two un-condensable turn-1 messages (Doer seed = convo[1] and
system prompt = convo[0]) plus the reserved reply must NEVER sum past the
window. Before A1 the seed used 0.55×window and the sysprompt 0.6×window with
NEITHER reserving output → worst case 1.15×window. These pin the co-budget at
BOTH a small (32K) and a large (256K) window, and that the budgets scale.
"""
from __future__ import annotations

import importlib

import pytest

from aiforge_core.runtime import text_doer as td
from aiforge_core.runtime import chat_agent as ca


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for v in ("AIFORGE_LOCAL_CTX_WINDOW", "AIFORGE_SEED_BUDGET_FRAC",
              "AIFORGE_SYS_PROMPT_FRAC", "AIFORGE_LLM_MAX_TOKENS"):
        monkeypatch.delenv(v, raising=False)
    # Explicit window wins before any probe, so no network is touched; disable
    # auto-detect anyway for hermeticity.
    monkeypatch.setenv("AIFORGE_AUTODETECT_CTX", "0")
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    yield
    importlib.reload(rsmod)


def _reload_rs():
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)


# C1: extend to SMALL windows (≤16K) where the fixed 8000 floors + a
# full-window output reservation used to overflow window×4 (at 8K the sum was
# 52236 vs 32768). The invariant must hold at EVERY window.
@pytest.mark.parametrize(
    "window", [4096, 8192, 16384, 32768, 131072, 262144])
def test_seed_plus_sysprompt_plus_output_fit_window(monkeypatch, window):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", str(window))
    _reload_rs()
    from aiforge_core.config import runtime_settings
    max_out = int(runtime_settings.get("max_output_tokens"))
    # The actual output reservation the budgets use is capped at a window
    # fraction (C1) — the reply never eats more than that of a small window.
    out_chars = td._out_reserve_chars(window * 4, max_out * 4)
    total = td._seed_budget_chars() + ca._sys_prompt_budget_chars() + out_chars
    assert total <= window * 4, (
        f"overflow at {window}: seed={td._seed_budget_chars()} "
        f"sys={ca._sys_prompt_budget_chars()} out={out_chars} "
        f"sum={total} > {window * 4}")
    # Every component is non-negative and leaves real room.
    assert td._seed_budget_chars() >= 0
    assert ca._sys_prompt_budget_chars() >= 0


def test_budgets_scale_with_window(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    _reload_rs()
    seed_small = td._seed_budget_chars()
    sys_small = ca._sys_prompt_budget_chars()
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "262144")
    _reload_rs()
    # Both grow with the window (a 256K box actually gets a bigger budget).
    assert td._seed_budget_chars() > seed_small
    assert ca._sys_prompt_budget_chars() > sys_small
