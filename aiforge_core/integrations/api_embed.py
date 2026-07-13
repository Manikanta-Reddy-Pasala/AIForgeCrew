"""OpenAI-compatible embeddings backend — no HuggingFace download, no torch.

Selected by ``AIFORGE_EMBED_BACKEND=api`` (aliases: ``openai``, ``lmstudio``).
Hits a ``/v1/embeddings`` endpoint you ALREADY run — LM Studio, Ollama
(``/v1/embeddings``), llama.cpp server, or any OpenAI-compatible host — so the
semantic vector comes from a warm local server instead of a per-box
``sentence-transformers`` model that stalls downloading ``model.safetensors``
from the HF Hub.

Config (env):
  AIFORGE_EMBED_BACKEND=api
  AIFORGE_EMBED_API_MODEL   the embedding model id loaded on the server
                            (e.g. LM Studio 'text-embedding-nomic-embed-text-v1.5',
                            Ollama 'nomic-embed-text'). REQUIRED.
  AIFORGE_EMBED_API_URL     base URL; default = AIFORGE_LM_BASE_URL (…/v1) so it
                            reuses your configured LLM host.
  AIFORGE_EMBED_API_KEY     optional bearer token (default AIFORGE_LM_API_KEY).

Errors are LOUD (no silent hash fallback) — the write path degrades to
store-without-vector via sqlite_memory._safe_embed, same as the local backend.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

_DIM: int | None = None
_TIMEOUT = float(os.environ.get("AIFORGE_EMBED_API_TIMEOUT", "30"))


def selected() -> bool:
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "api", "openai", "lmstudio", "ollama")


def _endpoint() -> str:
    base = (os.environ.get("AIFORGE_EMBED_API_URL")
            or os.environ.get("AIFORGE_LM_BASE_URL")
            or "http://127.0.0.1:1234/v1").strip().rstrip("/")
    return base if base.endswith("/embeddings") else base + "/embeddings"


def _model() -> str:
    m = (os.environ.get("AIFORGE_EMBED_API_MODEL") or "").strip()
    if not m:
        raise RuntimeError(
            "AIFORGE_EMBED_BACKEND=api needs AIFORGE_EMBED_API_MODEL set to an "
            "embedding model loaded on your server (e.g. LM Studio "
            "'text-embedding-nomic-embed-text-v1.5', Ollama 'nomic-embed-text').")
    return m


def _ctx():
    # internal HTTPS hosts are often self-signed; default to NOT verifying unless
    # AIFORGE_LLM_SSL_VERIFY is truthy (mirrors the LLM client's TLS policy).
    verify = str(os.environ.get("AIFORGE_LLM_SSL_VERIFY", "")).strip().lower() \
        in ("1", "true", "yes", "on")
    if _endpoint().startswith("https") and not verify:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return None


def embed(text: str) -> list[float]:
    """One embedding vector via the /v1/embeddings API. Raises on any failure
    (loud, per the no-silent-degrade rule)."""
    body = json.dumps({"model": _model(), "input": (text or " ")}).encode()
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("AIFORGE_EMBED_API_KEY")
           or os.environ.get("AIFORGE_LM_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(_endpoint(), data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_ctx()) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — surface as a clear embed error
        raise RuntimeError(
            f"embeddings API call failed ({_endpoint()}, model={_model()!r}): "
            f"{exc}. Is the embedding model loaded on the server?") from exc
    try:
        vec = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"embeddings API returned an unexpected shape: {str(data)[:200]}"
        ) from exc
    vec = [float(x) for x in vec]
    global _DIM
    _DIM = len(vec)
    return vec


def dim() -> int:
    """Embedding dimension — probe once (a tiny call) and cache."""
    global _DIM
    if _DIM is None:
        embed("dimension probe")
    return int(_DIM or 0)


def reset_for_tests() -> None:
    global _DIM
    _DIM = None
