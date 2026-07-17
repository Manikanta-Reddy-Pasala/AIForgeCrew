"""Cave (the standard default) condenses EARLY (~40% full) so small local
models don't drift + invent file edits as live context grows; a strong model
with cave opted out keeps most of its window. Env override always wins."""
from __future__ import annotations

import aiforge_core.runtime.chat_agent._context._window as w


def test_cave_default_condenses_at_40(monkeypatch):
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert w._history_fraction("chat") == w._CAVE_CONDENSE_FRACTION == 0.40


def test_cave_opt_out_keeps_window(monkeypatch):
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    assert w._history_fraction("chat") == w._FULL_CONDENSE_FRACTION == 0.85


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.5")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    assert w._history_fraction("chat") == 0.5


def test_env_override_clamped(monkeypatch):
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.02")  # too low
    assert w._history_fraction("chat") == 0.15
    monkeypatch.setenv("AIFORGE_CTX_HISTORY_FRACTION", "0.99")  # too high
    assert w._history_fraction("chat") == 0.95


def test_cave_budget_smaller_than_full(monkeypatch):
    """The 0.40 vs 0.85 fraction actually shrinks the live-history budget on the
    SAME window when cave is on."""
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "131072")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "1")
    cave = w._ctx_budget_chars("chat")
    monkeypatch.setenv("AIFORGE_CAVE_MODE", "0")
    full = w._ctx_budget_chars("chat")
    assert cave < full
