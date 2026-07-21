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
