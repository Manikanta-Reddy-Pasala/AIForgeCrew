from __future__ import annotations


def _count_tokens(text: str) -> int:
    """Exact tokens via tiktoken cl100k_base; chars/4 fallback if not installed."""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except ImportError:
        return len(text) // 4
