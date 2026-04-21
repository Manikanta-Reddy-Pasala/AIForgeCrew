"""bge-reranker-v2-m3 rerank sidecar.

Serves cross-encoder rerank scores for the aiforge retrieval pipeline.

RAM note: on Apple Silicon, MPS/Metal caches intermediate tensors keyed by
input shape. Over time this cache bloats (observed 6+ GB after hours of
use). We:
  1. Clear MPS cache after every rerank call — bounds steady-state.
  2. Optionally hard-restart the process every RESTART_REQS requests to
     wipe any slow leak the cache-clear misses (default: 500, disable
     with RESTART_REQS=0).
  3. Support CPU-only via RERANK_DEVICE=cpu for hosts where MPS churn is
     worse than CPU latency.
"""
from __future__ import annotations

import os
import signal
import threading
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI
from pydantic import BaseModel
from FlagEmbedding import FlagReranker

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
USE_FP16 = os.environ.get("RERANK_FP16", "1") == "1"
DEVICE = os.environ.get("RERANK_DEVICE")  # None=auto, "cpu", "mps", "cuda"
RESTART_REQS = int(os.environ.get("RERANK_RESTART_REQS", "500"))

_reranker: FlagReranker | None = None
_load_lock = threading.Lock()
_req_count = 0
_req_count_lock = threading.Lock()


def _load():
    global _reranker
    with _load_lock:
        if _reranker is not None:
            return
        kwargs = {"use_fp16": USE_FP16}
        if DEVICE:
            kwargs["devices"] = [DEVICE]
        _reranker = FlagReranker(MODEL_NAME, **kwargs)


def _mps_empty_cache() -> None:
    """Best-effort MPS cache release. No-op on non-Apple-Silicon."""
    try:
        import torch
        if hasattr(torch, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(title="aiforge-rerank-sidecar", version="1.1.0", lifespan=lifespan)


class Candidate(BaseModel):
    id: str
    text: str


class RerankReq(BaseModel):
    query: str
    candidates: List[Candidate]


@app.post("/rerank")
def rerank(req: RerankReq):
    global _req_count
    if not req.candidates:
        return {"scores": [], "order": [], "ids_ordered": []}
    pairs = [[req.query, c.text] for c in req.candidates]
    scores = _reranker.compute_score(pairs, normalize=True)
    # compute_score returns float for single pair, list for multiple
    if isinstance(scores, float):
        scores = [scores]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    _mps_empty_cache()

    if RESTART_REQS > 0:
        with _req_count_lock:
            _req_count += 1
            count = _req_count
        if count >= RESTART_REQS:
            # launchd with KeepAlive will immediately spawn a fresh process.
            os.kill(os.getpid(), signal.SIGTERM)

    return {
        "scores": [float(s) for s in scores],
        "order": order,
        "ids_ordered": [req.candidates[i].id for i in order],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME, "fp16": USE_FP16,
            "device": DEVICE or "auto", "reqs_since_restart": _req_count}
