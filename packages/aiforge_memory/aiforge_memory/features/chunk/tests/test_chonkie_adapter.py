"""chonkie adapter contract + the _split_code backend switch in embed.py."""
from __future__ import annotations

from aiforge_memory.features.chunk import chonkie_adapter, embed


def test_supports_lang_mapping():
    assert chonkie_adapter.supports_lang("python")
    assert chonkie_adapter.supports_lang("TSX")
    assert not chonkie_adapter.supports_lang("doc-markdown")
    assert not chonkie_adapter.supports_lang("")


def test_split_code_falls_back_to_lines_without_chonkie(monkeypatch):
    """With the adapter unavailable, _split_code == the line-window splitter
    exactly (shape and content)."""
    monkeypatch.setattr(chonkie_adapter, "available", lambda: False)
    text = "\n".join(f"line {i}" for i in range(120))
    got = embed._split_code(text, file_path="x.py", lang="python")
    assert got == embed._split(text, file_path="x.py")


def test_split_code_forced_lines_backend(monkeypatch):
    monkeypatch.setattr(embed, "CHUNKER", "lines")
    text = "\n".join(f"line {i}" for i in range(60))
    got = embed._split_code(text, file_path="x.py", lang="python")
    assert got == embed._split(text, file_path="x.py")


def test_split_code_uses_adapter_when_available(monkeypatch):
    monkeypatch.setattr(embed, "CHUNKER", "auto")
    monkeypatch.setattr(chonkie_adapter, "available", lambda: True)
    monkeypatch.setattr(chonkie_adapter, "chunk_code",
                        lambda text, lang, chunk_tokens=512:
                        [(0, "def a(): ...", 1, 3)])
    got = embed._split_code("def a(): ...", file_path="x.py", lang="python")
    assert got == [(0, "def a(): ...", 1, 3)]


def test_split_code_adapter_error_falls_back(monkeypatch):
    monkeypatch.setattr(embed, "CHUNKER", "auto")
    monkeypatch.setattr(chonkie_adapter, "available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("tree-sitter grammar missing")

    monkeypatch.setattr(chonkie_adapter, "chunk_code", boom)
    text = "\n".join(f"line {i}" for i in range(60))
    got = embed._split_code(text, file_path="x.py", lang="python")
    assert got == embed._split(text, file_path="x.py")
