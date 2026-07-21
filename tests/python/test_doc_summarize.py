"""Map-reduce document summariser — auto-selects strategy by size.

The LLM is monkeypatched so the tests are deterministic and offline: each fake
`complete` call echoes a short marker so we can assert map (per-window) and
reduce (fold) both ran.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import doc_summarize as ds


def _fake_complete(calls):
    """Return a `complete` stub that records every call and returns a marker
    encoding whether it was a MAP (per-window) or REDUCE (fold) prompt."""
    def _c(role, messages, *, max_tokens=None, **kw):
        prompt = messages[0]["content"]
        calls.append((role, max_tokens, prompt))
        if prompt.startswith("Below are section summaries"):
            return "FINAL-FOLD"
        return "map-sum"
    return _c


def test_small_text_returned_verbatim_no_llm(monkeypatch):
    """A tiny doc must NOT trigger an LLM call — cheap passthrough."""
    calls: list = []
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete(calls))
    out = ds.summarize_text("short note about the deploy", role="chat")
    assert out == "short note about the deploy"
    assert calls == []


def test_large_text_maps_each_window_then_reduces(monkeypatch):
    calls: list = []
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete(calls))
    monkeypatch.setenv("AIFORGE_SUMMARY_WINDOW_CHARS", "500")
    # ~10 windows of 500 chars.
    big = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
    out = ds.summarize_text(big, role="chat")
    map_calls = [c for c in calls if not c[2].startswith("Below are section")]
    fold_calls = [c for c in calls if c[2].startswith("Below are section")]
    assert len(map_calls) >= 2          # each window summarised
    assert len(fold_calls) == 1         # one reduce fold
    assert out == "FINAL-FOLD"


def test_max_windows_cap_truncates(monkeypatch):
    calls: list = []
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete(calls))
    monkeypatch.setenv("AIFORGE_SUMMARY_WINDOW_CHARS", "200")
    monkeypatch.setenv("AIFORGE_SUMMARY_MAX_WINDOWS", "3")
    big = "\n".join(f"line {i} " + "y" * 90 for i in range(100))
    out = ds.summarize_text(big, role="chat")
    map_calls = [c for c in calls if not c[2].startswith("Below are section")]
    assert len(map_calls) <= 3          # capped
    assert "exceeded 3 summary windows" in out


def test_disabled_falls_back_to_excerpt(monkeypatch):
    calls: list = []
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete(calls))
    monkeypatch.setenv("AIFORGE_DOC_SUMMARY_ENABLED", "0")
    big = "z" * 20000
    out = ds.summarize_text(big, role="chat")
    assert calls == []                  # no LLM
    assert len(out) <= 6000             # excerpt fallback


def test_llm_failure_falls_back_to_excerpt(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", _boom)
    monkeypatch.setenv("AIFORGE_SUMMARY_WINDOW_CHARS", "500")
    big = "\n".join(f"line {i} " + "w" * 90 for i in range(60))
    out = ds.summarize_text(big, role="chat")
    assert out                          # never empty
    assert len(out) <= 6000             # fell back to excerpt


def test_parse_page_spec():
    from aiforge_core.runtime import doc_extract as de
    assert de.parse_page_spec("10-12", 20) == [9, 10, 11]
    assert de.parse_page_spec("3,5,7-9", 20) == [2, 4, 6, 7, 8]
    assert de.parse_page_spec("12", 20) == [11]
    assert de.parse_page_spec("18-15", 20) == [14, 15, 16, 17]   # reversed range
    assert de.parse_page_spec("50", 20) == []                    # out of range
    assert de.parse_page_spec("junk", 20) == []
    assert de.parse_page_spec("", 20) == []


def test_docx_page_segmentation(tmp_path):
    """docx splits into pages on manual page breaks; each page keeps its text."""
    docx = pytest.importorskip("docx")
    from aiforge_core.runtime import doc_extract as de
    d = docx.Document()
    d.add_paragraph("page one content")
    d.add_page_break()                      # manual break -> new page
    d.add_paragraph("page two content")
    d.add_page_break()
    d.add_paragraph("page three content")
    p = tmp_path / "multi.docx"
    d.save(str(p))
    pages = de.document_pages(str(p))
    assert len(pages) == 3
    assert "page one" in pages[0] and "page two" in pages[1] and "page three" in pages[2]


def test_extract_pages_selects_range(tmp_path):
    docx = pytest.importorskip("docx")
    from aiforge_core.runtime import doc_extract as de
    d = docx.Document()
    for i in range(1, 6):
        d.add_paragraph(f"content of page {i}")
        if i < 5:
            d.add_page_break()
    p = tmp_path / "five.docx"
    d.save(str(p))
    out = de.extract_pages(str(p), "", "2-3")
    assert "--- page 2 ---" in out and "--- page 3 ---" in out
    assert "content of page 2" in out and "content of page 3" in out
    assert "content of page 1" not in out and "content of page 5" not in out


def test_summarize_document_page_range(monkeypatch, tmp_path):
    """summarize_document(pages=...) extracts ONLY the range, then summarises."""
    docx = pytest.importorskip("docx")
    from aiforge_core.runtime import doc_summarize as ds
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete([]))
    monkeypatch.setenv("AIFORGE_SUMMARY_WINDOW_CHARS", "60")
    d = docx.Document()
    for i in range(1, 6):
        d.add_paragraph(f"detailed content of page {i} " + "z" * 80)
        if i < 5:
            d.add_page_break()
    p = tmp_path / "doc.docx"
    d.save(str(p))
    captured = {}
    orig = ds.summarize_text
    def _spy(text, role="chat"):
        captured["text"] = text
        return orig(text, role)
    monkeypatch.setattr(ds, "summarize_text", _spy)
    ds.summarize_document(str(p), role="chat", pages="2-3")
    assert "page 2" in captured["text"] and "page 3" in captured["text"]
    assert "page 5" not in captured["text"]      # only the requested range


def test_describe_upload_summarizes_large_doc(monkeypatch, tmp_path):
    """Integration: a large text attachment gets a SUMMARY, a small one stays
    raw — the size auto-selection in chat_media._summarize_or_excerpt."""
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        _fake_complete([]))
    monkeypatch.setenv("AIFORGE_SUMMARY_WINDOW_CHARS", "500")
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"row {i} " + "q" * 90 for i in range(80)))
    out = chat_media.describe_upload(str(big), "big.txt", "text/plain", "chat")
    assert out.startswith("SUMMARY (auto-generated")

    small = tmp_path / "small.txt"
    small.write_text("just a line")
    out2 = chat_media.describe_upload(str(small), "small.txt", "text/plain", "chat")
    assert out2 == "just a line"
