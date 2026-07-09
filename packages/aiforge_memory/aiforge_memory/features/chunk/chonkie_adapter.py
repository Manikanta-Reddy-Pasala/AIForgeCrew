"""Smart chunking adapters for memory ingestion.

Two backends, one tuple contract — ``(idx, text, line_start, line_end)``,
the exact shape the line-window splitter in ``embed.py`` produces, so the
caller swaps chunkers without touching downstream (WalkedChunk, Cypher
upsert, embed sidecar):

* DOC chunking — chonkie's ``RecursiveChunker`` (markdown headers →
  paragraphs → sentences). BASE chonkie only, no tree-sitter.
* CODE chunking — OUR OWN AST chunker over ``tree_sitter_language_pack``
  0.13 (the exact version aider pins, already a core dependency). Splits at
  TOP-LEVEL node boundaries (functions/classes/imports) and packs nodes to
  a token budget. This REPLACED chonkie's CodeChunker, which needs
  tslp>=1.x and therefore could never run alongside aider.

Both raise on failure; ``embed.py`` owns the line-window fallback, so
ingestion never breaks on a chunker problem.
"""
from __future__ import annotations

# walker lang → tree-sitter-language-pack language name
_LANG_MAP = {
    "python": "python", "java": "java", "javascript": "javascript",
    "typescript": "typescript", "tsx": "tsx", "go": "go",
    "rust": "rust", "c": "c", "cpp": "cpp", "csharp": "csharp",
    "ruby": "ruby", "php": "php", "kotlin": "kotlin",
}


# ── DOC chunking (chonkie base — no tree-sitter) ─────────────────────────

def doc_available() -> bool:
    try:
        import chonkie  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def chunk_doc(text: str, *,
              chunk_tokens: int = 512) -> list[tuple[int, str, int, int]]:
    """Structure-aware DOC chunks (1-based line numbers from char offsets)."""
    from chonkie import RecursiveChunker
    out: list[tuple[int, str, int, int]] = []
    for idx, ch in enumerate(RecursiveChunker(chunk_size=chunk_tokens)
                             .chunk(text)):
        ch_text = getattr(ch, "text", "") or ""
        if not ch_text.strip():
            continue
        start = int(getattr(ch, "start_index", 0) or 0)
        end = int(getattr(ch, "end_index", start + len(ch_text)) or 0)
        line_start = text.count("\n", 0, max(0, start)) + 1
        line_end = text.count("\n", 0, max(start, end - 1)) + 1
        out.append((idx, ch_text, line_start, line_end))
    if not out:
        raise ValueError("chonkie produced no doc chunks")
    return out


# ── CODE chunking (own AST packer over tslp 0.13) ────────────────────────

def available() -> bool:
    """CODE chunking backend probe — needs only the tslp already shipped as
    a core dep (aider pins ==0.13.0; ``get_parser`` exists there)."""
    try:
        from tree_sitter_language_pack import get_parser  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def supports_lang(lang: str) -> bool:
    return (lang or "").lower() in _LANG_MAP


def chunk_code(text: str, lang: str, *,
               chunk_tokens: int = 512) -> list[tuple[int, str, int, int]]:
    """AST-aware code chunks: split at TOP-LEVEL node boundaries (functions,
    classes, imports) and greedily pack consecutive nodes up to the token
    budget (~4 chars/token), so a chunk never cuts a function in half. A
    single oversized node (a huge class) becomes its own chunk — the embed
    layer's caps handle it. 1-based line numbers from tree-sitter points."""
    from tree_sitter_language_pack import get_parser
    parser = get_parser(_LANG_MAP[(lang or "").lower()])
    data = text.encode("utf-8", errors="replace")
    tree = parser.parse(data)
    nodes = [n for n in tree.root_node.children if n.end_byte > n.start_byte]
    if not nodes:
        raise ValueError("no top-level AST nodes")
    budget = max(400, chunk_tokens * 4)
    lines = text.splitlines()

    def _slice(row_a: int, row_b: int) -> str:      # inclusive rows, 0-based
        return "\n".join(lines[row_a:row_b + 1])

    out: list[tuple[int, str, int, int]] = []
    grp_start: int | None = None
    grp_end = -1
    grp_chars = 0
    idx = 0

    def _flush() -> None:
        nonlocal grp_start, grp_end, grp_chars, idx
        if grp_start is None:
            return
        chunk = _slice(grp_start, grp_end)
        if chunk.strip():
            out.append((idx, chunk, grp_start + 1, grp_end + 1))
            idx += 1
        grp_start, grp_end, grp_chars = None, -1, 0

    for n in nodes:
        row_a, row_b = n.start_point[0], n.end_point[0]
        n_chars = n.end_byte - n.start_byte
        if grp_start is not None and grp_chars + n_chars > budget:
            _flush()
        if grp_start is None:
            grp_start = row_a
        grp_end = max(grp_end, row_b)
        grp_chars += n_chars
        if grp_chars >= budget:
            _flush()
    _flush()
    if not out:
        raise ValueError("AST packer produced no chunks")
    return out


__all__ = ["available", "supports_lang", "chunk_code",
           "doc_available", "chunk_doc"]
