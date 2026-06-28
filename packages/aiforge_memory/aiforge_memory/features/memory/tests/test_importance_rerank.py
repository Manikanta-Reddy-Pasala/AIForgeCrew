"""Importance (salience) factors into rerank_by_recency (gap #6)."""
from __future__ import annotations

from aiforge_memory.features.memory import store


def _rows():
    # identical relevance + age → only importance differs
    base = {"score": 0.5, "created_at_epoch": None, "confidence": 1.0}
    return [
        {"id": "low", "importance": 0.0, **base},
        {"id": "mid", "importance": 0.5, **base},
        {"id": "high", "importance": 1.0, **base},
    ]


def test_importance_orders_results():
    out = store.rerank_by_recency(_rows(), now=1_000_000.0)
    assert [r["id"] for r in out] == ["high", "mid", "low"]


def test_high_importance_beats_low_with_equal_relevance():
    out = store.rerank_by_recency(_rows(), now=1_000_000.0)
    by = {r["id"]: r["final_score"] for r in out}
    assert by["high"] > by["mid"] > by["low"]


def test_missing_importance_is_neutral():
    # a row with no importance key must score same as importance=0.5
    rows = [
        {"id": "none", "score": 0.5, "confidence": 1.0},
        {"id": "mid", "score": 0.5, "importance": 0.5, "confidence": 1.0},
    ]
    out = store.rerank_by_recency(rows, now=1_000_000.0)
    by = {r["id"]: r["final_score"] for r in out}
    assert abs(by["none"] - by["mid"]) < 1e-9


def test_importance_weight_zero_disables():
    out = store.rerank_by_recency(_rows(), now=1_000_000.0, w_importance=0.0)
    scores = {r["final_score"] for r in out}
    assert len(scores) == 1   # all equal when importance weight is off
