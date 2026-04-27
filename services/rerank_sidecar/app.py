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
DEVICE = os.environ.get("BGE_RERANKER_DEVICE", "")  # 'cpu' / 'cuda' / '' = auto
BATCH_SIZE = int(os.environ.get("BGE_RERANKER_BATCH", "16"))

_reranker = None
_load_lock = threading.Lock()


def _load() -> None:
    global _reranker
    with _load_lock:
        if _reranker is not None:
            return
        from FlagEmbedding import FlagReranker
        kwargs: dict = {"use_fp16": USE_FP16}
        if DEVICE:
            kwargs["devices"] = [DEVICE]
        _reranker = FlagReranker(MODEL_NAME, **kwargs)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load()
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
        "status": "ok",
        "model": MODEL_NAME,
        "loaded": _reranker is not None,
        "fp16": USE_FP16,
    }


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest) -> RerankResponse:
    if not req.texts:
        return RerankResponse(scores=[])
    if _reranker is None:
        _load()
    pairs = [[req.query, t] for t in req.texts]
    raw = _reranker.compute_score(
        pairs, normalize=True, batch_size=BATCH_SIZE,
    )
    if isinstance(raw, (int, float)):
        raw = [raw]
    return RerankResponse(scores=[float(x) for x in raw])
