"""Bug2 — cross-chat search: stemming broadens topic matches; density ranks
on-topic messages above incidental+recent ones."""
from __future__ import annotations

from aiforge_core.runtime.chat_store._helpers import _rank_search, _stem, _tokens


def test_stem_strips_common_inflections():
    assert _stem("deployment") == "deploy"
    # 'ies' strips to the BARE root, not '…y' — 'registr' is a substring of BOTH
    # 'registry' and 'registries' (the matcher is pure substring), so plural and
    # singular actually unify. 'registry' is NOT a substring of 'registries'.
    assert _stem("registries") == "registr"
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


def test_stem_unit_grouping_beats_repetition_spam():
    # raw token + its stem count as ONE unit toward 'matched', so a short
    # repetitive single-word row can't tie/outrank a genuine two-concept match.
    from aiforge_core.runtime.chat_store._helpers import _rank_search, _tokens
    rows = [
        {"id": 1, "session_id": 1, "role": "user",
         "content": "deployments deployments deployments", "created_at": 0},
        {"id": 2, "session_id": 2, "role": "user",
         "content": "we did the deployment and the rollback", "created_at": 0}]
    top = _rank_search(rows, _tokens("deployments rollback"), 2)
    assert top[0]["session_id"] == 2          # genuine 2-concept row wins


def test_stem_root_groups_non_idempotent_stem():
    # 'process' → 'proces' → 'proc': raw + its stem must land in ONE unit so a
    # process-spam row (one concept) can't tie a genuine two-concept match.
    from aiforge_core.runtime.chat_store._helpers import _rank_search, _tokens, _stem_root
    assert _stem_root("process") == _stem_root("proces")     # collapse to one root
    rows = [
        {"id": 1, "session_id": 1, "role": "user",
         "content": "process process process", "created_at": 0},
        {"id": 2, "session_id": 2, "role": "user",
         "content": "the process had errors today", "created_at": 0}]
    top = _rank_search(rows, _tokens("process errors"), 2)
    assert top[0]["session_id"] == 2          # two-concept row beats spam


def test_singular_query_finds_plural():
    # 'registry' now stems to the shared root so it matches stored 'registries'
    from aiforge_core.runtime.chat_store._helpers import _stem_root, _tokens
    assert _stem_root("registry") == _stem_root("registries")
    assert "registr" in _tokens("registry")       # base expands to the shared root


def test_stem_never_injects_a_stopword():
    # the trailing-'y' rule stems 'they'->'the'; 'the' must NOT become a token
    from aiforge_core.runtime.chat_store._helpers import _tokens
    assert "the" not in _tokens("they")
    assert "the" not in _tokens("how do they handle retries")


def test_round11_short_derivational_stem_no_overmatch():
    from aiforge_core.runtime.chat_store._helpers import (
        _rank_search, _stem, _stem_root, _tokens,
    )
    # server must NOT crush to the 3-4 char root "serv" that substring-hits
    # preserve/observe/conserve.
    assert _stem("servers") == "server"
    assert _stem_root("server") == "server"
    assert _stem_root("servers") != "serv"
    # …but the plural still unifies with the singular.
    assert _stem_root("servers") == _stem_root("server")
    # ranking: the doc actually about a server beats the spurious substring doc.
    def _row(i, text):
        return {"id": i, "session_id": "s", "session_title": "t", "role": "user",
                "content": text, "created_at": "2026-01-01T00:00:00+00:00"}
    rows = [
        _row("b", "preserve observe conserve"),
        _row("a", "restart the server"),
    ]
    ranked = _rank_search(rows, _tokens("servers"), 10)
    assert ranked and ranked[0]["content"] == "restart the server"


def test_round11_common_stems_still_unify():
    from aiforge_core.runtime.chat_store._helpers import _stem_root
    for a, b in (("tests", "test"), ("bugs", "bug"), ("fixed", "fix"),
                 ("deployment", "deployed"), ("registries", "registry")):
        assert _stem_root(a) == _stem_root(b), (a, b)


def test_round12_y_floor_reverted_keeps_singular_plural_symmetry():
    from aiforge_core.runtime.chat_store._helpers import _stem_root, _rank_search, _tokens
    # the round-11 y-floor-5 regression: 4-letter -y nouns must still unify with
    # their -ies plural (else a "body" query MISSES a "bodies" doc entirely).
    for sing, plur in (("body", "bodies"), ("copy", "copies"), ("city", "cities")):
        assert _stem_root(sing) == _stem_root(plur), (sing, plur)
    # and the er/ers fix is still in place (server not crushed to serv).
    assert _stem_root("server") == "server"
    assert _stem_root("servers") == "server"
    # end-to-end: a "body" query finds a request/response bodies doc.
    def _row(i, text):
        return {"id": i, "session_id": "s", "session_title": "t", "role": "user",
                "content": text, "created_at": "2026-01-01T00:00:00+00:00"}
    rows = [_row("a", "parse the request bodies and response bodies")]
    ranked = _rank_search(rows, _tokens("body"), 10)
    assert ranked and ranked[0]["content"].startswith("parse the request bodies")
