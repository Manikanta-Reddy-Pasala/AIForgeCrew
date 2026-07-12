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
