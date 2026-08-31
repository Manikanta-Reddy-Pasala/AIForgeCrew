"""External docs index: chunk → embed → persist → cosine lookup.

The module was at 0% coverage. These exercise the real SQLite round-trip with
a deterministic stub embedder, so the vector maths is checked for real rather
than mocked away.
"""
from __future__ import annotations

import sqlite3

import pytest

from aiforge_core.indexing import docs_index as di


@pytest.fixture
def docs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setenv("AIFORGE_DOCS_INDEX", "1")
    return di


def _stub_embed(monkeypatch, mapping=None, dim=4):
    """Deterministic embedder: a word→vector map, else a length-derived vector."""
    def _embed(text: str):
        if mapping:
            for k, v in mapping.items():
                if k in text:
                    return list(v)
        n = len(text) % 7 + 1
        return [float(n)] * dim
    monkeypatch.setattr("aiforge_core.memory.embed.embed", _embed)
    return _embed


# ── pure helpers ──────────────────────────────────────────────────────


def test_vec_blob_roundtrip_is_lossless_to_float32(docs):
    vec = [0.5, -1.25, 3.0, 0.0]
    assert docs._blob_to_vec(docs._vec_to_blob(vec)) == vec


def test_cosine_of_identical_vectors_is_one(docs):
    assert docs._cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero(docs):
    assert docs._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_guards_length_mismatch_and_zero_vectors(docs):
    assert docs._cosine([1.0, 2.0], [1.0]) == 0.0      # mismatch, not a crash
    assert docs._cosine([0.0, 0.0], [1.0, 2.0]) == 0.0  # zero norm


def test_chunk_strips_html_and_collapses_whitespace(docs):
    out = docs._chunk("<p>Hello   <b>world</b>.</p>\n\n Next.", chunk_chars=1000)
    assert len(out) == 1
    assert "<" not in out[0][1]
    assert "Hello world" in out[0][1]


def test_chunk_splits_on_the_size_budget(docs):
    text = " ".join(f"Sentence number {i} here." for i in range(40))
    small = docs._chunk(text, chunk_chars=60)
    assert len(small) > 1, "a tight budget must produce several chunks"
    assert all(body.strip() for _, body in small)


def test_chunk_of_empty_text_yields_nothing_or_blank(docs):
    assert docs._chunk("", chunk_chars=100) in ([], [("", "")])


# ── ingest ────────────────────────────────────────────────────────────


def test_ingest_persists_a_row_per_chunk(docs, monkeypatch):
    _stub_embed(monkeypatch)
    monkeypatch.setattr(docs, "_fetch", lambda url: "One. Two. Three.")
    added = docs.ingest("spring", ["http://example.invalid/a"], chunk_chars=10)
    assert added >= 2

    conn = sqlite3.connect(str(docs._root() / "spring.db"))
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    embeds = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    conn.close()
    assert chunks == added
    assert embeds == added, "every chunk must get an embedding row"


def test_ingest_is_a_no_op_when_the_feature_is_off(docs, monkeypatch):
    monkeypatch.setenv("AIFORGE_DOCS_INDEX", "0")
    called = []
    monkeypatch.setattr(docs, "_fetch", lambda url: called.append(url) or "x.")
    assert docs.ingest("spring", ["http://example.invalid/a"]) == 0
    assert not called, "the toggle must short-circuit before any fetch"


def test_ingest_skips_a_url_that_fails_to_fetch(docs, monkeypatch):
    _stub_embed(monkeypatch)

    def _fetch(url):
        if "bad" in url:
            raise OSError("boom")
        return "Good one. Good two."
    monkeypatch.setattr(docs, "_fetch", _fetch)
    added = docs.ingest("react", ["http://x/bad", "http://x/ok"], chunk_chars=10)
    assert added >= 1, "one bad URL must not lose the good one"


def test_ingest_skips_a_chunk_whose_embedding_fails(docs, monkeypatch):
    monkeypatch.setattr(docs, "_fetch", lambda url: "Alpha. Beta.")
    calls = {"n": 0}

    def _embed(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("embed down")
        return [1.0, 2.0]
    monkeypatch.setattr("aiforge_core.memory.embed.embed", _embed)
    added = docs.ingest("mongodb", ["http://x/a"], chunk_chars=6)
    assert added >= 1
    conn = sqlite3.connect(str(docs._root() / "mongodb.db"))
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == added
    conn.close()


# ── lookup ────────────────────────────────────────────────────────────


def test_lookup_returns_the_nearest_chunk_first(docs, monkeypatch):
    _stub_embed(monkeypatch, mapping={
        "cat": [1.0, 0.0], "dog": [0.0, 1.0], "kitten": [0.9, 0.1]})
    monkeypatch.setattr(docs, "_fetch", lambda url: "cat. dog.")
    docs.ingest("zoo", ["http://x/a"], chunk_chars=4)

    hits = docs.lookup_doc("zoo", "kitten", top_k=2)
    assert hits, "expected ranked hits"
    assert "cat" in hits[0]["text"], "nearest neighbour must rank first"
    assert hits[0]["score"] >= hits[-1]["score"]


def test_lookup_respects_top_k(docs, monkeypatch):
    _stub_embed(monkeypatch)
    monkeypatch.setattr(docs, "_fetch", lambda url: "A one. B two. C three. D four.")
    docs.ingest("lib", ["http://x/a"], chunk_chars=7)
    assert len(docs.lookup_doc("lib", "query", top_k=2)) <= 2


def test_lookup_of_a_blank_query_returns_empty(docs):
    assert docs.lookup_doc("anything", "   ") == []


def test_lookup_of_an_unknown_library_returns_empty(docs):
    assert docs.lookup_doc("never-ingested", "q") == []


def test_lookup_returns_empty_when_the_embedder_is_down(docs, monkeypatch):
    _stub_embed(monkeypatch)
    monkeypatch.setattr(docs, "_fetch", lambda url: "Some text.")
    docs.ingest("lib", ["http://x/a"])

    def _boom(text):
        raise RuntimeError("embed down")
    monkeypatch.setattr("aiforge_core.memory.embed.embed", _boom)
    assert docs.lookup_doc("lib", "q") == [], "a dead embedder degrades, not raises"


def test_lookup_skips_rows_whose_vector_cannot_be_compared(docs, monkeypatch):
    _stub_embed(monkeypatch, dim=2)
    monkeypatch.setattr(docs, "_fetch", lambda url: "Alpha.")
    docs.ingest("lib", ["http://x/a"])
    # Corrupt one embedding to a different width; it must be skipped, not fatal.
    conn = sqlite3.connect(str(docs._root() / "lib.db"))
    conn.execute("UPDATE embeddings SET vec = ?", (b"\x00\x01\x02",))
    conn.commit(); conn.close()
    assert isinstance(docs.lookup_doc("lib", "q"), list)


# ── library listing ───────────────────────────────────────────────────


def test_list_libraries_is_sorted_and_reflects_ingests(docs, monkeypatch):
    _stub_embed(monkeypatch)
    monkeypatch.setattr(docs, "_fetch", lambda url: "Text.")
    assert docs.list_libraries() == []
    docs.ingest("spring", ["http://x/a"])
    docs.ingest("react", ["http://x/b"])
    assert docs.list_libraries() == ["react", "spring"]
