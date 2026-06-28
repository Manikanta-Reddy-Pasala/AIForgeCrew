"""N7 — the cross-encoder must score path + chunk content, not bare
path strings, while still returning paths in the new order."""
from __future__ import annotations

from unittest.mock import patch

from aiforge_memory.query import translator


class _FakeResponse:
    def __init__(self, scores):
        self._scores = scores
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"scores": self._scores}


def test_rerank_doc_combines_path_and_chunk_text():
    doc = translator._rerank_doc("a/b.py", {"a/b.py": "def f():\n    pass"})
    assert doc.startswith("a/b.py\n")
    assert "def f():" in doc


def test_rerank_doc_caps_chunk_text_at_512():
    doc = translator._rerank_doc("a.py", {"a.py": "x" * 2000})
    assert len(doc) <= len("a.py\n") + 512


def test_rerank_doc_falls_back_to_bare_path():
    assert translator._rerank_doc("a.py", {}) == "a.py"


def test_rerank_posts_doc_texts_but_returns_paths():
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(json)
        return _FakeResponse(scores=[0.1, 0.9])

    docs = ["low.py", "high.py"]
    doc_texts = ["low.py\nirrelevant()", "high.py\ndef target(): ..."]
    with patch.object(translator.httpx, "post", side_effect=fake_post):
        out = translator._rerank(query="target fn", docs=docs,
                                 doc_texts=doc_texts)
    assert sent["texts"] == doc_texts            # content went over the wire
    assert out == ["high.py", "low.py"]          # but paths came back


def test_rerank_mismatched_doc_texts_falls_back_to_docs():
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(json)
        return _FakeResponse(scores=[0.5])

    with patch.object(translator.httpx, "post", side_effect=fake_post):
        translator._rerank(query="q", docs=["a.py"], doc_texts=["x", "y"])
    assert sent["texts"] == ["a.py"]
