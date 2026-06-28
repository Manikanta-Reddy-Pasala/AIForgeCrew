"""External-source ingest spine — chunking + source resolution unit
tests. The Neo4j round-trip is exercised in the live-Neo4j memory
tests; the slice covered here is pure-Python and doesn't need a DB."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiforge_memory.features.external_ingest import ingest
from aiforge_memory.features.external_ingest.ingest import (
    _chunk, _resolve_source, ingest_external_source,
)


def test_chunk_empty_returns_empty() -> None:
    assert _chunk("") == []
    assert _chunk("   \n  \n") == []


def test_chunk_single_paragraph_fits_target() -> None:
    text = "Hello world. " * 5
    chunks = _chunk(text, target=200)
    assert chunks == [text.strip()]


def test_chunk_splits_on_blank_lines_when_overflowing() -> None:
    text = ("A" * 600 + "\n\n" + "B" * 600 + "\n\n" + "C" * 600).strip()
    chunks = _chunk(text, target=700)
    assert len(chunks) == 3
    assert chunks[0].startswith("A")
    assert chunks[1].startswith("B")
    assert chunks[2].startswith("C")


def test_chunk_merges_small_paragraphs() -> None:
    text = "p1\n\np2\n\np3\n\np4"
    chunks = _chunk(text, target=200)
    # All 4 short paras fit in one chunk.
    assert chunks == ["p1\n\np2\n\np3\n\np4"]


def test_resolve_source_local_file(tmp_path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("# title\n\nbody text", encoding="utf-8")
    text, kind, uri = _resolve_source(str(p))
    assert kind == "file"
    assert uri == str(p)
    assert "body text" in text


def test_resolve_source_raw_text() -> None:
    text, kind, uri = _resolve_source("just a raw paragraph")
    assert kind == "raw"
    assert uri is None
    assert text == "just a raw paragraph"


def test_resolve_source_http_failure_raises() -> None:
    # A definitely-unreachable URL surfaces as RuntimeError so the
    # caller can soft-fail rather than crash.
    with pytest.raises(RuntimeError):
        _resolve_source("http://127.0.0.1:1/should-not-resolve")


def test_ingest_returns_errors_on_empty_source(monkeypatch) -> None:
    out = ingest_external_source(
        driver=MagicMock(),
        source="   ",
        repo="test_repo",
    )
    assert out["ok"] is False
    assert "empty_source" in out["errors"]


def test_ingest_writes_doc_plus_notes(monkeypatch, tmp_path) -> None:
    """Happy path: file source → 1 Doc + N Notes via the AFM store
    upserts (mocked)."""
    p = tmp_path / "doc.md"
    p.write_text(
        "Title paragraph.\n\n"
        + ("body " * 250) + "\n\n"
        + ("more " * 250),
        encoding="utf-8",
    )

    fake_doc = MagicMock(return_value={"id": "d1", "label": "Doc_v2"})
    fake_note = MagicMock(side_effect=[
        {"id": "n1", "label": "Note_v2"},
        {"id": "n2", "label": "Note_v2"},
        {"id": "n3", "label": "Note_v2"},
    ])
    fake_store = MagicMock(upsert_doc=fake_doc, upsert_note=fake_note)
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_store},
    ):
        out = ingest_external_source(
            driver=MagicMock(),
            source=str(p),
            repo="test_repo",
            source_type="manual",
            title="my doc",
            tags=["ingest-test"],
        )
    assert out["ok"] is True
    assert out["doc_id"] == "d1"
    assert len(out["note_ids"]) >= 1
    assert all(nid.startswith("n") for nid in out["note_ids"])
    # source_type, ingest_kind, uri tags propagate to every note
    for call in fake_note.call_args_list:
        tags = call.kwargs["tags"]
        assert "source_type:manual" in tags
        assert any(t.startswith("ingest_kind:") for t in tags)


def test_ingest_falls_back_to_notes_when_doc_upsert_fails(
    monkeypatch, tmp_path,
) -> None:
    """Doc upsert failure is non-fatal — we still write the notes so
    a transient Doc bug doesn't drop the body content."""
    p = tmp_path / "doc.md"
    p.write_text("short body", encoding="utf-8")

    fake_doc = MagicMock(side_effect=RuntimeError("doc cypher bang"))
    fake_note = MagicMock(return_value={"id": "n1", "label": "Note_v2"})
    fake_store = MagicMock(upsert_doc=fake_doc, upsert_note=fake_note)
    with patch.dict(
        "sys.modules",
        {"aiforge_memory.features.memory.store": fake_store},
    ):
        out = ingest_external_source(
            driver=MagicMock(),
            source=str(p),
            repo="test_repo",
        )
    assert out["ok"] is True
    assert out["doc_id"] is None
    assert "n1" in out["note_ids"]
    assert any("upsert_doc_failed" in e for e in out["errors"])
