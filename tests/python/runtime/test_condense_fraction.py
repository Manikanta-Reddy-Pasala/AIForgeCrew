"""Auto-compaction fires at 80% of the model's window (user, 2026-09-11).

It used to be 40% in cave mode (the default), which on a 256K model compacted
at ~96K — and the chat meter showed that 96K as if it were the window. Cave
still keeps context lean; it no longer halves the window. The env override
always wins, and the model's reply always keeps its room."""
from __future__ import annotations

import pytest

import aiforge_core.runtime.chat_agent._context._window as w


@pytest.mark.parametrize("cave", ["0", "1"])
def test_compaction_is_at_80_percent_in_and_out_of_cave(monkeypatch, cave):
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setenv("AIFORGE_CAVE_MODE", cave)
    assert w._history_fraction("chat") == w._CONDENSE_FRACTION == 0.80


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.5")
    assert w._history_fraction("chat") == 0.5


def test_env_override_clamped(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.02")  # too low
    assert w._history_fraction("chat") == 0.15
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.99")  # too high
    assert w._history_fraction("chat") == 0.95


def test_on_a_256k_model_the_context_compacts_near_205k(monkeypatch):
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 262144)
    sys_chars = 60000
    history = w._ctx_budget_chars("chat", sys_chars=sys_chars)
    compact_at_tokens = (history + sys_chars) // 4
    assert compact_at_tokens == int(262144 * 4 * 0.80) // 4      # ≈ 209.7K, not 96K


def test_the_reply_always_keeps_its_room_on_a_small_window(monkeypatch):
    """At 32K, 80% would leave less than the 8K output cap: the ceiling is the
    window minus the output cap instead."""
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.95")
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 32768)
    from aiforge_core.config import runtime_settings
    out_chars = int(runtime_settings.get("max_output_tokens")) * 4
    history = w._ctx_budget_chars("chat", sys_chars=14000)
    assert history + 14000 + out_chars <= 32768 * 4
