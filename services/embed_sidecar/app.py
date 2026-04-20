"""bge-m3 ONNX embedding sidecar.

Serves 1024-d dense embeddings for the aiforge memory store.
Loads model on startup. Single process recommended (holds ~2GB).
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

MODEL_DIR = os.environ.get("BGE_M3_DIR", os.path.expanduser("~/.aiforge/models/bge-m3"))
MAX_LEN = int(os.environ.get("BGE_M3_MAX_LEN", "512"))

_tokenizer = None
_session = None
_load_lock = threading.Lock()


def _load():
    global _tokenizer, _session
    with _load_lock:
        if _session is not None:
            return
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        _session = ort.InferenceSession(
            os.path.join(MODEL_DIR, "model.onnx"),
            providers=providers,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(title="aiforge-embed-sidecar", version="1.0.0", lifespan=lifespan)


class EmbedReq(BaseModel):
    text: str


class EmbedBatchReq(BaseModel):
    texts: List[str]


def _embed_batch(texts: List[str]) -> List[List[float]]:
    enc = _tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=MAX_LEN,
        return_tensors="np",
    )
    inputs = {
        "input_ids": enc["input_ids"].astype(np.int64),
        "attention_mask": enc["attention_mask"].astype(np.int64),
    }
    outputs = _session.run(None, inputs)
    # bge-m3 dense head output: last_hidden_state[:, 0, :]  (CLS pooling)
    cls = outputs[0][:, 0, :]
    # L2 normalize for cosine
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb = cls / norms
    return emb.astype(np.float32).tolist()


@app.post("/embed")
def embed(req: EmbedReq):
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    [v] = _embed_batch([req.text])
    return {"embedding": v}


@app.post("/embed_batch")
def embed_batch(req: EmbedBatchReq):
    if not req.texts:
        return {"embeddings": []}
    return {"embeddings": _embed_batch(req.texts)}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model_dir": MODEL_DIR, "dim": 1024}
