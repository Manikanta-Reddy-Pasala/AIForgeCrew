"""bge-reranker-v2-m3 cross-encoder sidecar via FlagEmbedding.

Serves cross-encoder reranking on :8765. Loads BAAI/bge-reranker-v2-m3
(PyTorch ~568 MB) on startup. Used by aiforge_core.memory.unified_query
to rerank top-30 retrieval hits → top-K via natural-language relevance.

Endpoints:
  POST /rerank   {query: str, texts: [str, ...]} → {scores: [float, ...]}
  GET  /healthz
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel

MODEL_NAME = os.environ.get("BGE_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
USE_FP16 = os.environ.get("BGE_RERANKER_FP16", "1") == "1"
DEVICE = os.environ.get("BGE_RERANKER_DEVICE", "")  # "cpu", "cuda", or empty to auto-detect
BATCH_SIZE = int(os.environ.get("BGE_RERANKER_BATCH", "16"))

_reranker = None
_load_lock = threading.Lock()
_load_error: "str | None" = None   # set when the model can't be loaded


def _load() -> None:
    global _reranker, _load_error
    with _load_lock:
        if _reranker is not None:
            return
        try:
            from FlagEmbedding import FlagReranker
            kwargs: dict = {"use_fp16": USE_FP16}
            if DEVICE:
                kwargs["devices"] = [DEVICE]
            _reranker = FlagReranker(MODEL_NAME, **kwargs)
            _load_error = None
        except Exception as exc:  # noqa: BLE001
            # The model isn't pre-staged (network lockdown = no runtime HF
            # download). Do NOT crash — the sidecar stays up + returns 503 so
            # reranking degrades gracefully (recall still works, just unranked).
            _load_error = (
                f"reranker model {MODEL_NAME!r} not available: {exc}. "
                "Pre-stage it into the hf-cache (./data/hf-cache) or set "
                "BGE_RERANKER_MODEL to a local dir; the sidecar stays up + "
                "returns 503 until then.")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # NEVER raise out of startup — a crash makes the container crash-loop under
    # restart:unless-stopped and spam logs. Load best-effort; report via
    # /healthz, 503 from /rerank.
    try:
        _load()
    except Exception as exc:  # noqa: BLE001
        global _load_error
        _load_error = f"reranker load failed: {exc}"
        import logging
        logging.getLogger("aiforge.rerank").warning(_load_error)
    yield


app = FastAPI(lifespan=_lifespan)


class RerankRequest(BaseModel):
    query: str
    texts: List[str]


class RerankResponse(BaseModel):
    scores: List[float]


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok" if _reranker is not None else "degraded",
        "model": MODEL_NAME,
        "loaded": _reranker is not None,
        "load_error": _load_error,
        "fp16": USE_FP16,
    }


# `response_model=` would only repeat the return annotation below, which
# FastAPI already reads. The 503 is real and was undeclared: a client
# reading the schema was told this call cannot fail.
@app.post("/rerank",
          responses={503: {"description": "Reranker unavailable"}})
def rerank(req: RerankRequest) -> RerankResponse:
    if not req.texts:
        return RerankResponse(scores=[])
    if _reranker is None:
        _load()
    if _reranker is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503,
                            detail=_load_error or "reranker unavailable")
    pairs = [[req.query, t] for t in req.texts]
    raw = _reranker.compute_score(
        pairs, normalize=True, batch_size=BATCH_SIZE,
    )
    if isinstance(raw, (int, float)):
        raw = [raw]
    return RerankResponse(scores=[float(x) for x in raw])
