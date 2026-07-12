"""LOCAL semantic embedder (sentence-transformers).

The production memory embedder. A small LOCAL sentence-transformer (on disk, no
network at query time) produces real semantic vectors so paraphrase recall +
cross-brief dedup work. Selected by ``AIFORGE_EMBED_BACKEND=semantic``.

NO SILENT FALLBACK: when the semantic backend is selected and the model can't
load, this RAISES — production must not quietly degrade to the lexical hash
embedder. The dependency-free hash path (``memory.local_embed`` +
``AIFORGE_EMBED_BACKEND=hash``, the default) exists only as the explicit dev/test
backend, not a fallback. Model id is config-driven (``AIFORGE_EMBED_MODEL``);
nothing is hardcoded to a machine.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger("aiforge.semantic_embed")

_MODEL = None
_DIM: int | None = None


def selected() -> bool:
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "semantic", "st", "sentence-transformers")


def _load():
    """Load (once) the configured model. RAISES if it can't — no fallback."""
    global _MODEL, _DIM
    if _MODEL is not None:
        return _MODEL
    from sentence_transformers import SentenceTransformer   # required dep
    name = os.environ.get(
        "AIFORGE_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    device = os.environ.get("AIFORGE_EMBED_DEVICE", "cpu")
    _MODEL = SentenceTransformer(name, device=device)
    # dimension: newer sentence-transformers renamed the accessor — try both,
    # else probe with a tiny encode.
    if hasattr(_MODEL, "get_embedding_dimension"):
        _DIM = int(_MODEL.get_embedding_dimension())
    elif hasattr(_MODEL, "get_sentence_embedding_dimension"):
        _DIM = int(_MODEL.get_sentence_embedding_dimension())
    else:
        _DIM = int(len(_MODEL.encode("x", normalize_embeddings=True)))
    _log.info("semantic embedder loaded: %s (dim=%d, %s)", name, _DIM, device)
    return _MODEL


def dim() -> int:
    _load()
    return int(_DIM)


def embed(text: str) -> list[float]:
    """Semantic vector for ``text``. RAISES if the model is unavailable."""
    m = _load()
    v = m.encode((text or "").strip(), normalize_embeddings=True)
    return [float(x) for x in v]


def reset_for_tests() -> None:
    global _MODEL, _DIM
    _MODEL, _DIM = None, None


__all__ = ["embed", "dim", "selected", "reset_for_tests"]
