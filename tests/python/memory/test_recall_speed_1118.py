"""Recall must not wait on a slow reranker every turn, nor embed one query twice."""
import socket

from aiforge_core.memory.unified_query import _ranking as R


def test_a_timed_out_reranker_is_skipped_for_a_while(monkeypatch):
    calls = []

    def slow(url, query, hits):
        calls.append(1)
        raise socket.timeout("timed out")
    monkeypatch.setattr(R, "_fetch_rerank_scores", slow)
    monkeypatch.setattr(R, "_RERANK_DOWN_UNTIL", [0.0])
    hits = [{"text": "a", "score": 1.0}]
    assert R._rerank_top(hits, query="q") is None
    assert R._rerank_top(hits, query="q") is None
    assert len(calls) == 1                                  # second turn did not wait


def test_other_errors_do_not_trip_the_breaker(monkeypatch):
    monkeypatch.setattr(R, "_RERANK_DOWN_UNTIL", [0.0])
    monkeypatch.setattr(R, "_fetch_rerank_scores", lambda *a: (_ for _ in ()).throw(ValueError("bad json")))
    assert R._rerank_top([{"text": "a"}], query="q") is None
    assert R._RERANK_DOWN_UNTIL[0] == 0.0


def test_timeout_defaults_short_and_is_settable(monkeypatch):
    monkeypatch.delenv("AIFORGE_RERANK_TIMEOUT_S", raising=False)
    assert R._rerank_timeout() == 2.5
    monkeypatch.setenv("AIFORGE_RERANK_TIMEOUT_S", "6")
    assert R._rerank_timeout() == 6.0


def test_the_same_text_is_embedded_once(monkeypatch):
    from aiforge_core.memory import embed as side
    from aiforge_core.memory import local_embed as L
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "sidecar")
    L._RECENT.clear()
    n = []
    monkeypatch.setattr(side, "embed", lambda t: n.append(t) or [0.1, 0.2])
    assert L.embed("how do we deploy") == L.embed("how do we deploy") == [0.1, 0.2]
    assert len(n) == 1
    L.embed("other text")
    assert len(n) == 2
