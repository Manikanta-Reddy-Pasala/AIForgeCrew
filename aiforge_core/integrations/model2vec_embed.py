"""model2vec static-embedding backend — real semantic vectors, NO torch, tiny.

Selected by ``AIFORGE_EMBED_BACKEND=model2vec`` (alias ``static``). Uses a
distilled STATIC embedding model (minishlab/potion-base-8M, ~30 MB, pure-numpy
inference — no torch, no sentence-transformers). Fast enough to embed on every
write/recall, and real paraphrase quality (car↔automobile ≈ 0.75, car↔banana ≈
0.06) — the kind of semantic recall the hash backend can't do.

Why this over the ``semantic`` backend: no torch (~GB) and a 3× smaller model,
so it's the practical semantic option when the sentence-transformers download
stalls. And it loads from a LOCAL directory — download the ~30 MB model once
(anywhere), copy the folder to the box, point ``AIFORGE_EMBED_MODEL2VEC_PATH``
at it, and there is ZERO network at runtime.

Config (env):
  AIFORGE_EMBED_BACKEND=model2vec
  AIFORGE_EMBED_MODEL2VEC_PATH   local model dir (offline; no HF fetch), OR
  AIFORGE_EMBED_MODEL2VEC_MODEL  HF id (default minishlab/potion-base-8M).

Errors are LOUD (no silent hash fallback) — the write path degrades to
store-without-vector via sqlite_memory._safe_embed, same as the other backends.
"""
from __future__ import annotations

import os

_MODEL = None
_DIM: int | None = None
_LOAD_ERR: str | None = None


def _source() -> str:
    return (os.environ.get("AIFORGE_EMBED_MODEL2VEC_PATH")
            or os.environ.get("AIFORGE_EMBED_MODEL2VEC_MODEL")
            or "minishlab/potion-base-8M")


def _load():
    global _MODEL, _DIM, _LOAD_ERR
    if _MODEL is not None:
        return _MODEL
    if _LOAD_ERR is not None:                       # don't re-hang every call
        raise RuntimeError(_LOAD_ERR)
    src = _source()
    try:
        from model2vec import StaticModel           # required for this backend
    except Exception as exc:  # noqa: BLE001
        _LOAD_ERR = (f"model2vec not installed ({exc}). Install it: "
                     "uv pip install model2vec  (no torch).")
        raise RuntimeError(_LOAD_ERR) from exc
    try:
        _MODEL = StaticModel.from_pretrained(src)
        _DIM = int(len(_MODEL.encode(["x"])[0]))
    except Exception as exc:  # noqa: BLE001
        _MODEL = None
        _LOAD_ERR = (f"could not load model2vec model {src!r}: {exc}. "
                     "Set AIFORGE_EMBED_MODEL2VEC_PATH to a locally-copied model "
                     "dir to avoid the HF download.")
        raise RuntimeError(_LOAD_ERR) from exc
    return _MODEL


def embed(text: str) -> list[float]:
    m = _load()
    vec = m.encode([text or " "])[0]
    return [float(x) for x in vec]


def dim() -> int:
    if _DIM is None:
        _load()
    return int(_DIM or 0)


def selected() -> bool:
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "model2vec", "static")


def reset_for_tests() -> None:
    global _MODEL, _DIM, _LOAD_ERR
    _MODEL, _DIM, _LOAD_ERR = None, None, None
