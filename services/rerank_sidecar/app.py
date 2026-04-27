"""bge-reranker-v2-m3 ONNX cross-encoder sidecar.

Serves cross-encoder reranking on :8765. Loads model on startup
(~600 MB), single process. Used by aiforge_core.memory.unified_query
to rerank top-30 hits → top-K via natural-language relevance scores.

Endpoints:
  POST /rerank   {query: str, texts: [str, ...]} → {scores: [float, ...]}
  GET  /healthz
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import List

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoTokenizer

MODEL_DIR = os.environ.get(
    "BGE_RERANKER_DIR",
    os.path.expanduser("~/.aiforge/models/bge-reranker-v2-m3"),
)
MAX_LEN = int(os.environ.get("BGE_RERANKER_MAX_LEN", "512"))
BATCH_SIZE = int(os.environ.get("BGE_RERANKER_BATCH", "16"))

_tokenizer = None
_session = None
_load_lock = threading.Lock()


def _load() -> None:
    global _tokenizer, _session
    with _load_lock:
        if _session is not None:
            return
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        providers = ["CPUExecutionProvider"]
        # Optional CUDA / CoreML if available; falls back silently.
        try:
            available = ort.get_available_providers()
            for p in ("CUDAExecutionProvider", "CoreMLExecutionProvider"):
                if p in available:
                    providers.insert(0, p)
        except Exception:
            pass
        _session = ort.InferenceSession(
            os.path.join(MODEL_DIR, "model.onnx"),
            providers=providers,
        )


def _score_batch(query: str, texts: List[str]) -> List[float]:
    """Cross-encoder score per (query, text) pair."""
    pairs = [(query, t) for t in texts]
    enc = _tokenizer(
        pairs,
        padding=True,
        truncation=True,
        max_length=MAX_LEN,
        return_tensors="np",
    )
    feeds = {k: v for k, v in enc.items()
             if k in {n.name for n in _session.get_inputs()}}
    raw = _session.run(None, feeds)[0]
    # Logits → sigmoid → [0, 1] relevance scores.
    arr = np.asarray(raw).reshape(-1)
    return (1.0 / (1.0 + np.exp(-arr))).tolist()


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
    return {"status": "ok", "model_dir": MODEL_DIR, "loaded": _session is not None}


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest) -> RerankResponse:
    if not req.texts:
        return RerankResponse(scores=[])
    if _session is None:
        _load()
    out: List[float] = []
    for i in range(0, len(req.texts), BATCH_SIZE):
        chunk = req.texts[i:i + BATCH_SIZE]
        out.extend(_score_batch(req.query, chunk))
    return RerankResponse(scores=out)
