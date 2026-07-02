"""M1 — `_normalize_scores` must PRESERVE an absolute relevance floor.

Pure min-max per-source normalization promoted a weak single-hit source to
norm 1.0 (× weight) so a marginal raw=0.20 singleton `doc` outranked strong
raw=0.80+ `memory` facts, and drove the lowest of a tight strong band to 0.0.
These pin the corrected blend + single-hit behaviour.

M2 — full-text dedup: two distinct facts that share a 200-char boilerplate
prefix must BOTH survive; genuine duplicates still merge to the higher score.

M4 — the chat source must be able to exclude the current live session.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def uq():
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    return uq


def _sorted(uq, hits):
    out = uq._normalize_scores(hits)
    out.sort(key=lambda h: -h["score"])
    return out


# ── M1: weak singleton must NOT outrank a strong band ────────────────────────

def test_weak_singleton_does_not_outrank_strong_band(uq, monkeypatch):
    """The audit's exact scenario: a raw=0.20 singleton `doc` must NOT beat
    raw 0.83/0.82/0.80 `memory` facts (equal weights)."""
    monkeypatch.delenv("AIFORGE_UMEM_ABS_WEIGHT", raising=False)
    hits = [{"source": "doc", "text": "weak", "_raw_score": 0.20,
             "_weight": 1.0, "score": 0.20}]
    for i, raw in enumerate((0.83, 0.82, 0.80)):
        hits.append({"source": "memory", "text": f"M{i}", "_raw_score": raw,
                     "_weight": 1.0, "score": raw})
    out = _sorted(uq, hits)
    # Every memory fact ranks ABOVE the weak doc singleton.
    weak_pos = [h["text"] for h in out].index("weak")
    assert weak_pos == 3, [h["text"] for h in out]
    weak = [h for h in out if h["text"] == "weak"][0]
    assert all(h["score"] > weak["score"]
               for h in out if h["source"] == "memory")


def test_tight_strong_band_all_above_weak_singleton(uq, monkeypatch):
    """A tight strong band (0.80-0.85) keeps ALL members above a weak singleton
    — min-max used to sink the band's floor (0.80) to 0.0."""
    monkeypatch.delenv("AIFORGE_UMEM_ABS_WEIGHT", raising=False)
    hits = [{"source": "doc", "text": "weak", "_raw_score": 0.20,
             "_weight": 1.0, "score": 0.20}]
    for i, raw in enumerate((0.85, 0.83, 0.81, 0.80)):
        hits.append({"source": "memory", "text": f"M{i}", "_raw_score": raw,
                     "_weight": 1.0, "score": raw})
    out = _sorted(uq, hits)
    weak = [h for h in out if h["text"] == "weak"][0]
    band = [h for h in out if h["source"] == "memory"]
    assert all(h["score"] > weak["score"] for h in band), \
        [(h["text"], round(h["score"], 3)) for h in out]


def test_genuinely_strong_single_hit_still_ranks(uq, monkeypatch):
    """A strong single hit (raw 0.95) still ranks well vs a mediocre band."""
    monkeypatch.delenv("AIFORGE_UMEM_ABS_WEIGHT", raising=False)
    hits = [{"source": "afm_bundle", "text": "strong", "_raw_score": 0.95,
             "_weight": 1.0, "score": 0.95}]
    for i, raw in enumerate((0.30, 0.25, 0.20)):
        hits.append({"source": "memory", "text": f"M{i}", "_raw_score": raw,
                     "_weight": 1.0, "score": raw})
    out = _sorted(uq, hits)
    assert out[0]["text"] == "strong"


def test_normalize_off_leaves_scores(uq, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_NORMALIZE", "0")
    hits = [{"source": "doc", "text": "x", "_raw_score": 0.2,
             "_weight": 1.0, "score": 0.7}]
    out = uq._normalize_scores(hits)
    assert out[0]["score"] == 0.7  # untouched


# ── M2: full-text dedup keeps distinct facts with a shared prefix ────────────

def test_dedup_shared_prefix_distinct_full_text_both_kept(uq):
    prefix = "BOILERPLATE " * 20  # >200 chars
    a = prefix + "the sync loop uses NATS JetStream"
    b = prefix + "the pull loop paginates by updatedAt"
    hits = [{"source": "memory", "text": a, "score": 0.8},
            {"source": "memory", "text": b, "score": 0.7}]
    out = uq._dedup(list(hits))
    texts = [h["text"] for h in out]
    assert a in texts and b in texts
    assert len(out) == 2


def test_dedup_same_full_text_merges_higher(uq):
    body = "identical fact body here"
    hits = [{"source": "doc", "text": body, "score": 0.4},
            {"source": "afm_bundle", "text": body, "score": 0.9}]
    out = uq._dedup(list(hits))
    assert len(out) == 1
    assert out[0]["score"] == 0.9


def test_dedup_same_source_uri_merges(uq):
    hits = [{"source": "doc", "text": "one wording", "score": 0.4,
             "source_uri": "afm://repo/note/1"},
            {"source": "afm_bundle", "text": "other wording", "score": 0.9,
             "source_uri": "afm://repo/note/1"}]
    out = uq._dedup(list(hits))
    assert len(out) == 1
    assert out[0]["score"] == 0.9


# ── M4: chat source excludes the current session ─────────────────────────────

def test_query_threads_exclude_session_to_chat(uq, monkeypatch):
    captured = {}

    def _fake_search(text, *, limit=6, exclude_session=None):
        captured["exclude"] = exclude_session
        return []

    import aiforge_core.runtime.chat_store as cs
    monkeypatch.setattr(cs, "search_messages", _fake_search)
    monkeypatch.setenv("AIFORGE_UMEM_CHAT", "1")
    # Only exercise the chat source path.
    uq._chat_sessions("how did we wire sync", limit=4, exclude_session=5)
    assert captured["exclude"] == 5


def test_query_exclude_session_default_none(uq, monkeypatch):
    captured = {}

    def _fake_search(text, *, limit=6, exclude_session=None):
        captured["exclude"] = exclude_session
        return []

    import aiforge_core.runtime.chat_store as cs
    monkeypatch.setattr(cs, "search_messages", _fake_search)
    res = uq.query("some query text", exclude_session=5)
    # query() ran without error and returned the standard envelope.
    assert "hits" in res
