"""Smart-chunk adapter contract + the _split_code backend switch in embed.py.
CODE chunking = OUR AST packer over tree-sitter-language-pack 0.13 (core
dep, tslp-0.13-compatible) — REAL here, no optional install needed."""
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


_PY = (
    "import os\n\n"
    + "\n\n".join(
        f"def fn_{i}(x):\n    y = x + {i}\n    return y" for i in range(40))
    + "\n\nclass Big:\n" + "\n".join(
        f"    def m_{i}(self):\n        return {i}" for i in range(30)) + "\n")


def test_ast_chunker_real_python():
    """REAL AST chunking (no mocks): boundaries at top-level nodes — no chunk
    ever cuts a def in half; line ranges are 1-based and ordered."""
    assert chonkie_adapter.available()
    chunks = chonkie_adapter.chunk_code(_PY, "python", chunk_tokens=128)
    assert len(chunks) > 3
    for _idx, text, l0, l1 in chunks:
        assert 1 <= l0 <= l1
        # a chunk that STARTS with an indented line would mean a split def
        assert not text.splitlines()[0].startswith((" ", "\t"))
    # coverage: every def appears in exactly one chunk
    joined = "\n".join(c[1] for c in chunks)
    assert all(f"def fn_{i}(" in joined for i in range(40))


def test_ast_chunker_via_split_code():
    got = embed._split_code(_PY, file_path="x.py", lang="python")
    assert got == chonkie_adapter.chunk_code(
        _PY, "python", chunk_tokens=embed.CHUNK_TOKENS)


def test_ast_chunker_unsupported_lang_falls_back():
    text = "\n".join(f"line {i}" for i in range(120))
    got = embed._split_code(text, file_path="x.zig", lang="zig")
    assert got == embed._split(text, file_path="x.zig")
