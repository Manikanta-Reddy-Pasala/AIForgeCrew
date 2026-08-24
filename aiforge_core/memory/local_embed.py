"""Offline, dependency-free hash embedding for the SQLite memory backend.

No model, no network — a deterministic feature-hashing vectorizer over word +
character-trigram tokens with sublinear term frequency and L2 normalization.
This gives *lexical* similarity recall (shared words/morphology rank higher),
the zero-dependency default. For real paraphrase (semantic) recall without
torch, use the ``model2vec`` backend (static embeddings) or ``api`` (an
OpenAI-compatible /v1/embeddings endpoint) — see :func:`embed`.

Properties relied on by the store and tests:
  * deterministic — same text -> same vector across processes/runs
  * fixed dimension ``EMBED_DIM``
  * L2-normalized (so dot product == cosine)
"""
from __future__ import annotations

import hashlib
import math
import re

EMBED_DIM = 256

_WORD_RE = re.compile(r"[a-z0-9]+")


def _bucket(token: str) -> int:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % EMBED_DIM


def _tokens(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    toks: list[str] = list(words)
    # Character trigrams of each word add morphological overlap so
    # "configure" and "configuring" share signal.
    for w in words:
        if len(w) <= 3:
            toks.append(f"#{w}")
            continue
        padded = f"^{w}$"
        for i in range(len(padded) - 2):
            toks.append(f"#{padded[i:i+3]}")
    return toks


def _backend() -> str:
    import os as _os
    return _os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower()


def embed_signature() -> str:
    """A cheap identity of the ACTIVE embedder — ``backend:model`` — so a switch
    is detected even when the new backend has the SAME dimension (hash and
    model2vec are both 256-dim, so a dim-only check would miss hash↔model2vec and
    leave stale vectors). No model load: reads env only."""
    import os as _os
    b = _backend()
    if b in ("model2vec", "static"):
        return "model2vec:" + (_os.environ.get("AIFORGE_EMBED_MODEL2VEC_PATH")
                               or _os.environ.get("AIFORGE_EMBED_MODEL2VEC_MODEL")
                               or "minishlab/potion-base-8M")
    if b in ("api", "openai", "lmstudio", "ollama"):
        return "api:" + (_os.environ.get("AIFORGE_EMBED_API_MODEL") or "")
    return "hash"


def embed(text: str) -> list[float]:
    """The active embedder (RAISES on failure for the real backends — no silent
    hash fallback; the write path degrades via ``_safe_embed``):
      * ``model2vec`` / ``static`` → distilled STATIC embeddings (~30 MB, NO
        torch; real paraphrase quality). Loads from a local dir → zero network.
      * ``api`` / ``openai`` / ``lmstudio`` / ``ollama`` → an OpenAI-compatible
        ``/v1/embeddings`` endpoint (reuses your model server; NO HF download).
      * default ``hash`` → the dependency-free lexical embedder below."""
    b = _backend()
    if b in ("model2vec", "static"):
        from aiforge_core.integrations import model2vec_embed as _m2
        return _m2.embed(text)
    if b in ("api", "openai", "lmstudio", "ollama"):
        from aiforge_core.integrations import api_embed as _ae
        return _ae.embed(text)
    return _hash_embed(text)


def embed_dim() -> int:
    """Dimension of the ACTIVE embedder."""
    b = _backend()
    if b in ("model2vec", "static"):
        from aiforge_core.integrations import model2vec_embed as _m2
        return _m2.dim()
    if b in ("api", "openai", "lmstudio", "ollama"):
        from aiforge_core.integrations import api_embed as _ae
        return _ae.dim()
    return EMBED_DIM


def _hash_embed(text: str) -> list[float]:
    """Dependency-free lexical (feature-hashing) embedder — the dev/test backend.
    Return an L2-normalized ``EMBED_DIM``-length float vector; empty input → zero
    vector (cosine 0 = non-match)."""
    vec = [0.0] * EMBED_DIM
    counts: dict[str, int] = {}
    for tok in _tokens(text or ""):
        counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return vec
    for tok, c in counts.items():
        # Sublinear TF damps very repetitive tokens.
        vec[_bucket(tok)] += 1.0 + math.log(c)
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two same-length vectors. For L2-normalized inputs
    this equals cosine similarity. Returns 0.0 on length mismatch."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
