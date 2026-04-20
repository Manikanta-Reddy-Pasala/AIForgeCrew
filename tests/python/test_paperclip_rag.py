from pathlib import Path
from aiforge_core.rag import _chunk_generic, _chunk_for_path


def test_chunk_generic_char_overlap():
    text = "a" * 6000
    chunks = _chunk_generic(text)
    assert len(chunks) >= 2
    # Overlap present: chunk[1] starts within chunk[0]
    assert chunks[1][:100] in chunks[0] + text


def test_python_chunker_splits_by_def(monkeypatch):
    # Force fallback (no tree-sitter) by raising on import
    import builtins
    real_import = builtins.__import__

    def raising_import(name, *a, **kw):
        if name == "tree_sitter_python":
            raise ImportError
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", raising_import)
    from aiforge_core.rag import _chunk_python
    text = "def a(): pass\n\ndef b(): pass\n"
    out = _chunk_python(text)
    assert out
    assert any("def a" in c[1] or "def b" in c[1] for c in out)
