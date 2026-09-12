"""What recall is allowed to put in front of the model when it has no answer.

Four gates, each of which was open:
  * the graphify channel resolved ITS OWN checkout when the request had no repo
    scope, so AIForge's internals were injected into every unscoped chat;
  * per-channel score scaling grouped by the stored ``source`` (unique per row),
    so it never actually ran;
  * the ``recent`` hot cache scored by recency alone — the newest row scored
    0.7 on every query, above a real cosine hit;
  * nothing dropped a weak cosine hit, so a question with no match still came
    back with the least-bad rows.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def uq():
    import aiforge_core.memory.unified_query as _uq
    importlib.reload(_uq)
    return _uq


# ── the graphify channel needs a repo scope ──────────────────────────────────

def test_graphify_refuses_when_the_request_has_no_repo(monkeypatch):
    """No repo root ⇒ no graph: the walk-up fallback found AIForge's own
    graphify-out/ and passed its call graph off as project knowledge."""
    from aiforge_core.runtime import graphify_lookup_tool as glt
    from aiforge_core.runtime import request_context
    monkeypatch.delenv("AIFORGE_REPO_ROOT", raising=False)
    monkeypatch.setattr(request_context, "get_repo_root", lambda: None)
    res = glt.graphify_lookup("Memory")
    assert res["ok"] is False
    assert "repo" in res["error"]


def test_graphify_uses_the_requests_repo_when_scoped(monkeypatch, tmp_path):
    import json

    from aiforge_core.runtime import graphify_lookup_tool as glt
    from aiforge_core.runtime import request_context
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(
        {"nodes": [{"id": "n1", "label": "Memory", "source_file": "m.py"}],
         "links": []}))
    monkeypatch.setattr(request_context, "get_repo_root", lambda: str(tmp_path))
    glt._CACHE.clear()
    res = glt.graphify_lookup("Memory")
    assert res["ok"] is True
    assert [m["label"] for m in res["matches"]] == ["Memory"]


# ── scaling groups by CHANNEL, not by the row's stored source ────────────────

def test_scaling_groups_by_channel_not_stored_source(uq, monkeypatch):
    """Two rows of one channel whose stored sources differ must scale TOGETHER.
    Grouped by ``source`` each was a band of one (span 0) and kept its raw
    score, so the weak row rode along with the strong one."""
    monkeypatch.delenv("AIFORGE_UMEM_ABS_WEIGHT", raising=False)
    hits = [{"channel": "memory", "source": "compacted:alpha", "text": "strong",
             "_raw_score": 0.90, "_weight": 1.0, "score": 0.90},
            {"channel": "memory", "source": "compacted:beta", "text": "weak",
             "_raw_score": 0.30, "_weight": 1.0, "score": 0.30}]
    out = {h["text"]: h["score"] for h in uq._normalize_scores(hits)}
    # blended: strong → norm 1.0, weak → norm 0.0 (0.5*0.30 + 0.5*0)
    assert out["strong"] > 0.9 > out["weak"]
    assert out["weak"] == pytest.approx(0.15, abs=1e-6)


def test_rows_without_a_channel_group_by_source_as_before(uq, monkeypatch):
    monkeypatch.delenv("AIFORGE_UMEM_ABS_WEIGHT", raising=False)
    hits = [{"source": "memory", "text": "a", "_raw_score": 0.80,
             "_weight": 1.0, "score": 0.80},
            {"source": "memory", "text": "b", "_raw_score": 0.60,
             "_weight": 1.0, "score": 0.60}]
    out = {h["text"]: h["score"] for h in uq._normalize_scores(hits)}
    assert out["a"] > out["b"]


# ── the relevance floor ──────────────────────────────────────────────────────

def test_a_weak_cosine_hit_is_dropped_not_returned_as_filler(uq, monkeypatch):
    monkeypatch.delenv("AIFORGE_UMEM_MIN_RELEVANCE", raising=False)
    hits = [{"channel": "memory", "text": "noise", "_raw_score": 0.04,
             "_weight": 1.0, "score": 0.04}]
    assert uq._normalize_scores(hits) == []


def test_the_floor_is_tunable_and_can_be_switched_off(uq, monkeypatch):
    hits = [{"channel": "memory", "text": "noise", "_raw_score": 0.04,
             "_weight": 1.0, "score": 0.04}]
    monkeypatch.setenv("AIFORGE_UMEM_MIN_RELEVANCE", "0")
    assert len(uq._normalize_scores(list(hits))) == 1
    monkeypatch.setenv("AIFORGE_UMEM_MIN_RELEVANCE", "0.5")
    assert uq._normalize_scores(list(hits)) == []


def test_rank_scored_channels_are_not_floored(uq, monkeypatch):
    """A keyword hit's score is its RANK among real matches, and a ticket's is
    a constant — a low number there does not mean 'irrelevant'."""
    monkeypatch.delenv("AIFORGE_UMEM_MIN_RELEVANCE", raising=False)
    hits = [{"channel": "keyword", "text": "last bm25 row", "_raw_score": 0.0,
             "_weight": 0.9, "score": 0.0},
            {"channel": "ticket", "text": "ONE-1", "_raw_score": 1.0,
             "_weight": 1.2, "score": 1.2}]
    assert len(uq._normalize_scores(hits)) == 2


# ── the recent hot cache must be about the question ─────────────────────────

def test_recent_rows_that_say_nothing_about_the_query_are_dropped(uq):
    from aiforge_core.memory.unified_query import _query
    rows = [{"text": "installed prometheus on the metrics box", "score": 1.0},
            {"text": "the NATS retry backoff is 5s", "score": 0.5}]
    kept = _query._relevant_recent(rows, "what is the NATS retry backoff")
    assert [r["text"] for r in kept] == ["the NATS retry backoff is 5s"]


def test_a_partly_related_fresh_row_is_scaled_down_not_promoted(uq):
    """The newest row scores 1.0 by POSITION, which is what let the last thing
    written top every answer. Scaled by how much of the question it actually
    covers, a row that touches one word of three lands well below a real hit —
    while a row that answers the whole question keeps its place."""
    from aiforge_core.memory.unified_query import _query
    partial = _query._relevant_recent(
        [{"text": "the nats cluster moved to three nodes", "score": 1.0}],
        "what is the nats retry backoff")
    assert partial
    assert partial[0]["score"] == pytest.approx(1 / 3, abs=1e-6)
    full = _query._relevant_recent(
        [{"text": "nats retry backoff is 5s", "score": 1.0}],
        "what is the nats retry backoff")
    assert full
    assert full[0]["score"] == pytest.approx(1.0)


def test_recent_gate_is_tunable(uq, monkeypatch):
    from aiforge_core.memory.unified_query import _query
    rows = [{"text": "unrelated note about billing", "score": 1.0}]
    monkeypatch.setenv("AIFORGE_UMEM_RECENT_MIN_OVERLAP", "0")
    # still requires SOME overlap — a row sharing nothing is never evidence
    assert _query._relevant_recent(rows, "nats retry backoff") == []
    assert _query._relevant_recent(
        [{"text": "billing retry", "score": 1.0}], "billing") != []


def test_lexical_overlap_ignores_stopwords(uq):
    from aiforge_core.memory.unified_query._helpers import _lexical_overlap
    assert _lexical_overlap("how does the sync work", "the and for with") == 0.0
    assert _lexical_overlap("sync loop", "the sync loop uses NATS") == 1.0
