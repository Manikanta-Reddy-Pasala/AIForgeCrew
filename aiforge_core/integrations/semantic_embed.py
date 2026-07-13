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
_LOAD_ERR: Exception | None = None   # cache a load failure → don't retry per call


def selected() -> bool:
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "semantic", "st", "sentence-transformers")


def _load():
    """Load (once) the configured model. RAISES if it can't — no fallback.

    Fail-FAST + fail-ONCE: a box that can't reach huggingface.co otherwise hangs
    the model download at 0 B/s forever AND retries it on every embed (so every
    chat message hangs). We cap each download attempt with a short timeout and
    CACHE the failure so subsequent calls raise immediately with an actionable
    message instead of re-hanging."""
    global _MODEL, _DIM, _LOAD_ERR
    if _MODEL is not None:
        return _MODEL
    if _LOAD_ERR is not None:
        raise _LOAD_ERR                      # already failed once — don't re-hang
    name = os.environ.get(
        "AIFORGE_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    device = os.environ.get("AIFORGE_EMBED_DEVICE", "cpu")
    # Fail fast on a dead/blocked HF connection instead of stalling at 0 B/s.
    # huggingface_hub honours HF_HUB_DOWNLOAD_TIMEOUT (per-request seconds).
    os.environ.setdefault(
        "HF_HUB_DOWNLOAD_TIMEOUT",
        os.environ.get("AIFORGE_EMBED_DOWNLOAD_TIMEOUT", "30"))
    try:
        from sentence_transformers import SentenceTransformer   # required dep
        _MODEL = SentenceTransformer(name, device=device)
        # dimension: newer sentence-transformers renamed the accessor — try both,
        # else probe with a tiny encode.
        if hasattr(_MODEL, "get_embedding_dimension"):
            _DIM = int(_MODEL.get_embedding_dimension())
        elif hasattr(_MODEL, "get_sentence_embedding_dimension"):
            _DIM = int(_MODEL.get_sentence_embedding_dimension())
        else:
            _DIM = int(len(_MODEL.encode("x", normalize_embeddings=True)))
    except Exception as exc:  # noqa: BLE001 — cache + re-raise with guidance
        _MODEL = None
        _LOAD_ERR = RuntimeError(
            f"semantic embedder could not load model {name!r}: {exc}. This box "
            f"likely can't reach huggingface.co to download it. Fix: run with "
            f"AIFORGE_EMBED_BACKEND=hash (keyword recall, no model needed), OR "
            f"pre-download the model into ~/.cache/huggingface on a connected "
            f"machine and copy it over. Tune the timeout with "
            f"AIFORGE_EMBED_DOWNLOAD_TIMEOUT (seconds).")
        raise _LOAD_ERR from exc
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
    global _MODEL, _DIM, _LOAD_ERR
    _MODEL, _DIM, _LOAD_ERR = None, None, None


__all__ = ["embed", "dim", "selected", "reset_for_tests"]
