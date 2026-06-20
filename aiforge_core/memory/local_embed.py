"""Offline, dependency-free hash embedding for the SQLite memory backend.

No model, no sidecar, no network — a deterministic feature-hashing
vectorizer over word + character-trigram tokens with sublinear term
frequency and L2 normalization. This gives *lexical* similarity recall
(shared words/morphology rank higher) which is enough for the embedded
"runs anywhere" memory. Semantic recall remains the job of the bge-m3
sidecar + Neo4j/Postgres "pro" backends.

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


def embed(text: str) -> list[float]:
    """Return an L2-normalized ``EMBED_DIM``-length float vector.

    Empty / whitespace input returns a zero vector (cosine 0 with
    everything), which the store treats as a non-match.
    """
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
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two same-length vectors. For L2-normalized inputs
    this equals cosine similarity. Returns 0.0 on length mismatch."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
