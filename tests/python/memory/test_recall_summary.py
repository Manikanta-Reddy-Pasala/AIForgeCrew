"""T7 — map→summarize→LLM recall.

When a query pulls many scattered hits across sources, fold them into ONE compact
LLM briefing before injection (instead of dumping snippets). Below a threshold,
or on any failure, return "" so the caller keeps its raw ranked list.
"""
from __future__ import annotations

import pytest


def _hits(n):
    return [{"text": f"fact number {i}", "source": "memory", "score": 0.5}
            for i in range(n)]


def test_folds_when_many(monkeypatch):
    from aiforge_core.memory import recall_summary
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE", "1")
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE_MIN", "3")
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "- synthesized brief\n- second point")
    out = recall_summary.summarize_hits("data sync", _hits(5))
    assert "synthesized brief" in out


def test_skips_when_few(monkeypatch):
    from aiforge_core.memory import recall_summary
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE", "1")
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE_MIN", "5")
    assert recall_summary.summarize_hits("q", _hits(2)) == ""


def test_soft_fails_on_llm_error(monkeypatch):
    from aiforge_core.memory import recall_summary
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE", "1")
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE_MIN", "1")

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.client.complete", _boom)
    assert recall_summary.summarize_hits("q", _hits(4)) == ""


def test_disabled(monkeypatch):
    from aiforge_core.memory import recall_summary
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE", "0")
    assert recall_summary.summarize_hits("q", _hits(9)) == ""


# -- hang guard: the fold runs before the turn's first model call, so it must
# NOT inherit the global AIFORGE_LLM_TIMEOUT_S (900 s).

def _timeout_seen(monkeypatch, env_value):
    from aiforge_core.memory import recall_summary
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE", "1")
    monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE_MIN", "1")
    if env_value is None:
        monkeypatch.delenv("AIFORGE_UMEM_SUMMARIZE_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_UMEM_SUMMARIZE_TIMEOUT_S", env_value)
    seen = {}

    def _fake(*a, **k):
        seen.update(k)
        return "- brief"

    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake)
    recall_summary.summarize_hits("q", _hits(4))
    return seen.get("timeout_s")


@pytest.mark.parametrize("env_value, expected", [
    (None, 60), ("120", 120), ("0", 1), ("abc", 60),
])
def test_timeout_knob(monkeypatch, env_value, expected):
    assert _timeout_seen(monkeypatch, env_value) == expected


@pytest.mark.parametrize("fold", ["raise", "empty"])
def test_chat_recall_falls_back_to_raw_lines(monkeypatch, fold):
    """A timed-out / failed fold must leave the chat first-turn recall with
    the raw ranked lines — asserted through _recall._memory_recall."""
    from aiforge_core.runtime.chat_agent._context import _recall
    hits = [{"text": f"ranked fact {i}", "source": "memory"} for i in range(3)]
    monkeypatch.setattr(_recall, "_recall_hits", lambda *a, **k: hits)

    def _fold(*a, **k):
        if fold == "raise":
            raise TimeoutError("fold timed out")
        return ""

    monkeypatch.setattr(
        "aiforge_core.memory.recall_summary.summarize_hits", _fold)
    out = _recall._memory_recall("/tmp", "how is the sync wired?")
    assert out.startswith(_recall._RECALL_PREAMBLE)
    for i in range(3):
        assert f"- ranked fact {i}  (memory)" in out
