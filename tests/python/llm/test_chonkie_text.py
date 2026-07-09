"""Structure-aware text chunking for LLM-bound files — base-chonkie adapter,
the smart observation truncation, and the memory doc-chunker upgrade."""
from __future__ import annotations

import json

import pytest

from aiforge_core.integrations import chonkie_text_adapter as cta

_MD = ("# Guide\n\nIntro paragraph explaining the module.\n\n## Install\n\n"
       + "step alpha beta. " * 150
       + "\n\n## Usage\n\n" + "call the api like so. " * 120)


def test_chunk_text_structural():
    if not cta.available():
        pytest.skip("chonkie not installed")
    chunks = cta.chunk_text(_MD, chunk_tokens=120)
    assert len(chunks) > 2
    assert "".join(chunks) == _MD          # lossless partition


def test_cut_at_structure_never_mid_slice():
    if not cta.available():
        pytest.skip("chonkie not installed")
    cut = cta.cut_at_structure(_MD, 800)
    assert len(cut) <= 800
    assert _MD.startswith(cut)             # a clean prefix, boundary-aligned


def test_smart_truncate_obs_cuts_content_with_note(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    big = {"ok": True, "title": "Doc", "content": _MD * 3}
    out = ca._smart_truncate_obs(big, 4000)
    parsed = json.loads(out)               # STILL VALID JSON (old slice wasn't)
    assert "TRUNCATED at a structure boundary" in parsed["content"]
    assert "read_lines" in parsed["content"]
    assert parsed["title"] == "Doc"        # other fields intact


def test_smart_truncate_obs_small_results_untouched():
    from aiforge_core.runtime import chat_agent as ca
    small = {"ok": True, "content": "tiny"}
    assert json.loads(ca._smart_truncate_obs(small, 4000)) == small


def test_smart_truncate_fallback_without_chonkie(monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    monkeypatch.setattr(cta, "available", lambda: False)
    big = {"ok": True, "content": ("para one.\n\n" * 800)}
    out = ca._smart_truncate_obs(big, 3000)
    parsed = json.loads(out)
    assert "TRUNCATED at a structure boundary" in parsed["content"]


def test_memory_doc_chunker_uses_chonkie():
    from aiforge_memory.features.chunk import chonkie_adapter, embed
    if not chonkie_adapter.doc_available():
        pytest.skip("chonkie not installed")
    chunks = embed._split_doc_smart(_MD, file_path="README.md")
    assert len(chunks) > 2
    idx, text0, l0, _ = chunks[0]
    assert idx == 0 and text0.startswith("# Guide") and l0 == 1


def test_memory_doc_chunker_falls_back(monkeypatch):
    from aiforge_memory.features.chunk import chonkie_adapter, embed
    monkeypatch.setattr(chonkie_adapter, "doc_available", lambda: False)
    got = embed._split_doc_smart(_MD, file_path="README.md")
    assert got == embed._split_doc(_MD, file_path="README.md")
