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
_load_error: str | None = None   # set when the model dir is missing/incomplete


def _load():
    global _tokenizer, _session, _load_error
    with _load_lock:
        if _session is not None:
            return
        onnx_path = os.path.join(MODEL_DIR, "model.onnx")
        # Clear, actionable failure instead of a crash-loop when the model
        # files were never provisioned (empty bind-mount). bge-m3 is a ~2GB
        # ONNX export the operator must supply at BGE_M3_DIR (model.onnx +
        # tokenizer files); set BGE_M3_HOST_DIR to a dir that has them.
        if not os.path.isfile(onnx_path):
            _load_error = (
                f"bge-m3 model not found at {MODEL_DIR} (missing model.onnx). "
                "Provision the bge-m3 ONNX export there or set BGE_M3_HOST_DIR "
                "to a dir containing it; the embed sidecar stays up but returns "
                "503 until then (semantic dedupe / vector recall degrade "
                "gracefully meanwhile)."
            )
            return
        # local_files_only: never reach out to the HF Hub — the model must be
        # pre-staged into MODEL_DIR. Network lockdown = no runtime downloads.
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        _session = ort.InferenceSession(onnx_path, providers=providers)
        _load_error = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # NEVER raise out of startup — a crash here makes the container crash-loop
    # (restart:unless-stopped) and spam logs on every host that hasn't staged
    # the model. Load best-effort; report status via /healthz, 503 from /embed.
    try:
        _load()
    except Exception as exc:  # noqa: BLE001
        global _load_error
        _load_error = f"bge-m3 load failed: {exc}"
        import logging
        logging.getLogger("aiforge.embed").warning(_load_error)
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
    # bge-m3 ONNX exports vary: some emit last_hidden_state [B, L, H] requiring
    # CLS slice; others expose already-pooled dense head [B, H]. Handle both.
    cls = outputs[0]
    if cls.ndim == 3:
        cls = cls[:, 0, :]
    # L2 normalize for cosine
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb = cls / norms
    return emb.astype(np.float32).tolist()


def _require_model():
    if _session is None:
        raise HTTPException(503, _load_error or "model not loaded")


# Both routes can answer 503 (the model was never staged — see _require_model)
# and /embed can answer 400. Declared here so the generated schema says so;
# a client reading only the OpenAPI doc was told these calls cannot fail.
@app.post("/embed", responses={
    400: {"description": "Empty text"},
    503: {"description": "Model not loaded"},
})
def embed(req: EmbedReq):
    _require_model()
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    [v] = _embed_batch([req.text])
    return {"embedding": v}


@app.post("/embed_batch",
          responses={503: {"description": "Model not loaded"}})
def embed_batch(req: EmbedBatchReq):
    _require_model()
    if not req.texts:
        return {"embeddings": []}
    return {"embeddings": _embed_batch(req.texts)}


@app.get("/healthz")
def healthz():
    return {"status": "ok" if _session is not None else "unconfigured",
            "model_dir": MODEL_DIR, "dim": 1024,
            "error": _load_error}
