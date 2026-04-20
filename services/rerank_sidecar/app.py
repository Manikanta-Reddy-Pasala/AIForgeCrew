"""bge-reranker-v2-m3 rerank sidecar.

Serves cross-encoder rerank scores for the aiforge retrieval pipeline.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel
from FlagEmbedding import FlagReranker

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
USE_FP16 = os.environ.get("RERANK_FP16", "1") == "1"

_reranker: FlagReranker | None = None
_load_lock = threading.Lock()


def _load():
    global _reranker
    with _load_lock:
        if _reranker is not None:
            return
        _reranker = FlagReranker(MODEL_NAME, use_fp16=USE_FP16)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(title="aiforge-rerank-sidecar", version="1.0.0", lifespan=lifespan)


class Candidate(BaseModel):
    id: str
    text: str


class RerankReq(BaseModel):
    query: str
    candidates: List[Candidate]


@app.post("/rerank")
def rerank(req: RerankReq):
    if not req.candidates:
        return {"scores": [], "order": [], "ids_ordered": []}
    pairs = [[req.query, c.text] for c in req.candidates]
    scores = _reranker.compute_score(pairs, normalize=True)
    # compute_score returns float for single pair, list for multiple
    if isinstance(scores, float):
        scores = [scores]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return {
        "scores": [float(s) for s in scores],
        "order": order,
        "ids_ordered": [req.candidates[i].id for i in order],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME, "fp16": USE_FP16}
