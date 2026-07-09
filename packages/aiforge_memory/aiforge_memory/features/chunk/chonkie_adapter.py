"""chonkie adapter — AST-aware code chunking (``pip install
aiforge-memory[chunking]``).

Same adapter contract as aiforge_core/integrations: ``available()`` probe +
one narrow capability typed on OUR shapes. :func:`chunk_code` returns the
exact tuple shape the line-window splitter in ``embed.py`` produces —
``(idx, text, line_start, line_end)`` — so the caller swaps chunkers without
touching downstream (WalkedChunk, Cypher upsert, embed sidecar).

Raises on any failure; ``embed.py`` owns the fallback to line windows.
"""
from __future__ import annotations

# walker lang → tree-sitter language name chonkie understands
_LANG_MAP = {
    "python": "python", "java": "java", "javascript": "javascript",
    "typescript": "typescript", "tsx": "typescript", "go": "go",
    "rust": "rust", "c": "c", "cpp": "cpp", "csharp": "c_sharp",
    "ruby": "ruby", "php": "php", "kotlin": "kotlin",
}


def available() -> bool:
    """Probe the EXACT symbols CodeChunker needs — not just ``import chonkie``.
    In the monorepo root env aider-chat pins an older
    tree-sitter-language-pack that lacks ``download_all``; there chonkie
    imports fine but CodeChunker raises at construction, so a bare import
    probe would advertise a backend that can never work."""
    try:
        import chonkie  # noqa: F401
        from tree_sitter_language_pack import (  # noqa: F401
            download_all, downloaded_languages)
        return True
    except Exception:  # noqa: BLE001
        return False


def supports_lang(lang: str) -> bool:
    return (lang or "").lower() in _LANG_MAP


def chunk_code(text: str, lang: str, *,
               chunk_tokens: int = 512) -> list[tuple[int, str, int, int]]:
    """AST-aware chunks for ``text``. Line numbers are derived from chonkie's
    character offsets so they stay 1-based like the line-window splitter."""
    from chonkie import CodeChunker

    ts_lang = _LANG_MAP[(lang or "").lower()]
    chunker = CodeChunker(language=ts_lang, chunk_size=chunk_tokens)
    out: list[tuple[int, str, int, int]] = []
    for idx, ch in enumerate(chunker.chunk(text)):
        ch_text = getattr(ch, "text", "") or ""
        if not ch_text.strip():
            continue
        start = int(getattr(ch, "start_index", 0) or 0)
        end = int(getattr(ch, "end_index", start + len(ch_text)) or 0)
        line_start = text.count("\n", 0, max(0, start)) + 1
        line_end = text.count("\n", 0, max(start, end - 1)) + 1
        out.append((idx, ch_text, line_start, line_end))
    if not out:
        raise ValueError("chonkie produced no chunks")
    return out


__all__ = ["available", "supports_lang", "chunk_code"]
