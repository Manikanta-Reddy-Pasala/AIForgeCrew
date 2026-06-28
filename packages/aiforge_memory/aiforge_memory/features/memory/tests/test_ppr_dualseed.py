"""Dual-seed PPR scoring helpers (gap #7, pure)."""
from __future__ import annotations

from aiforge_memory.features.memory import store


def test_norm_scores_scales_to_unit():
    n = store._norm_scores([{"id": "a", "score": 2.0}, {"id": "b", "score": 1.0}])
    assert n == {"a": 1.0, "b": 0.5}
    assert store._norm_scores([]) == {}


def test_blend_seed_only_no_overlap():
    # seed with no neighbours still surfaces (vec_score = seed score)
    out = store._blend_ppr({"a": 1.0}, [], [{"id": "a", "text": "t", "kind": "note", "tags": []}],
                           alpha=0.6, k=10)
    assert out[0]["id"] == "a"
    assert out[0]["vec_score"] == 1.0
    assert out[0]["overlap_score"] == 0.0
    assert abs(out[0]["score"] - 0.6) < 1e-9   # alpha*1 + (1-alpha)*0


def test_blend_overlap_lifts_non_seed():
    # 'b' is not a seed but shares 2 neighbours; with low alpha overlap wins
    seed = {"a": 1.0}
    cand = [{"id": "b", "text": "tb", "kind": "note", "tags": [], "overlap": 2},
            {"id": "c", "text": "tc", "kind": "note", "tags": [], "overlap": 1}]
    srows = [{"id": "a", "text": "ta", "kind": "note", "tags": []}]
    out = store._blend_ppr(seed, cand, srows, alpha=0.3, k=10)
    ids = [r["id"] for r in out]
    assert set(ids) == {"a", "b", "c"}
    by = {r["id"]: r for r in out}
    assert by["b"]["overlap_score"] == 1.0   # 2/2 max
    assert by["c"]["overlap_score"] == 0.5   # 1/2
    assert "overlap" not in by["b"]          # internal field stripped


def test_blend_topk_truncates():
    seed = {f"s{i}": 1.0 - i * 0.1 for i in range(5)}
    srows = [{"id": f"s{i}", "text": "t", "kind": "n", "tags": []} for i in range(5)]
    out = store._blend_ppr(seed, [], srows, alpha=1.0, k=2)
    assert len(out) == 2
    assert out[0]["id"] == "s0"   # highest seed score first
