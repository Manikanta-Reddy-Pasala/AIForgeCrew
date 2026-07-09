"""chonkie TEXT adapter — structure-aware chunking of text/docs for the LLM
(``pip install aiforgecrew[chunking]`` → base ``chonkie``, NO tree-sitter).

Distinct from the memory package's CodeChunker adapter: chonkie's
``RecursiveChunker`` splits on document structure (markdown headers →
paragraphs → sentences → tokens) and needs NO tree-sitter — so unlike the
CodeChunker (blocked by aider's tree-sitter-language-pack==0.13.0 exact
pin) it works in the ROOT env, today, alongside aider.

Used wherever a FILE/page/document is being sent to the model and must be
cut to a budget: cutting at a structure boundary keeps the last section
intact instead of slicing mid-sentence/mid-JSON. Raises on failure — the
caller owns the plain-slice fallback.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import chonkie  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def chunk_text(text: str, *, chunk_tokens: int = 512) -> list[str]:
    """Structure-aware chunks (markdown/paragraph/sentence boundaries)."""
    from chonkie import RecursiveChunker
    chunks = [c.text for c in RecursiveChunker(chunk_size=chunk_tokens)
              .chunk(text) if (getattr(c, "text", "") or "").strip()]
    if not chunks:
        raise ValueError("chonkie produced no chunks")
    return chunks


def cut_at_structure(text: str, max_chars: int) -> str:
    """The largest prefix of ``text`` that fits ``max_chars`` while ending on
    a STRUCTURE boundary (never mid-sentence). Raises on failure."""
    if len(text) <= max_chars:
        return text
    # ~4 chars/token; chunk small enough that boundaries land often.
    parts = chunk_text(text, chunk_tokens=max(64, max_chars // 16))
    out: list[str] = []
    used = 0
    for p in parts:
        if used + len(p) > max_chars:
            break
        out.append(p)
        used += len(p)
    if not out:                      # first chunk alone exceeds the budget
        raise ValueError("no structural prefix fits the budget")
    return "".join(out)


__all__ = ["available", "chunk_text", "cut_at_structure"]
