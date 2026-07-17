"""Bug2 — cross-chat search: stemming broadens topic matches; density ranks
on-topic messages above incidental+recent ones."""
from __future__ import annotations

from aiforge_core.runtime.chat_store._helpers import _rank_search, _stem, _tokens


def test_stem_strips_common_inflections():
    assert _stem("deployment") == "deploy"
    assert _stem("registries") == "registry"
    # "running" -> strips "ing"; stem differs and stays >= 3 chars
    assert _stem("running") != "running" and len(_stem("running")) >= 3
    # short words untouched
    assert _stem("api") == "api"


def test_tokens_include_stem_variants():
    toks = _tokens("deployment errors")
    # both the raw token and its stem present so a substring scan matches
    # "deploy"/"deployed" and "error"/"errors"
    assert "deployment" in toks and "deploy" in toks
    assert "errors" in toks and "error" in toks


def test_stem_matches_shorter_word_form():
    # A prior message said "deploy"; a query for "deployment" must now hit it
    # via the stemmed token.
    rows = [{"id": 1, "session_id": 9, "role": "assistant",
             "content": "we deploy via the NATS retry stream",
             "created_at": "2026-01-01T00:00:00", "session_title": "t"}]
    out = _rank_search(rows, _tokens("deployment"), 5)
    assert len(out) == 1


def test_density_outranks_recency():
    toks = _tokens("cache")
    dense_old = {"id": 1, "session_id": 1, "role": "assistant",
                 "content": "cache cache cache invalidation strategy for the cache",
                 "created_at": "2026-01-01", "session_title": "t"}
    incidental_new = {"id": 999, "session_id": 2, "role": "assistant",
                      "content": "mentioned the cache once in passing",
                      "created_at": "2026-02-01", "session_title": "t"}
    out = _rank_search([incidental_new, dense_old], toks, 5)
    # the on-topic (denser) message wins despite the other being newer (higher id)
    assert out[0]["content"].startswith("cache cache cache")
