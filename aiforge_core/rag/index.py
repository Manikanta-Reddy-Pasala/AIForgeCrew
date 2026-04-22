"""Build a read-only LlamaIndex VectorStoreIndex over the existing memories table."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llama_index.core import VectorStoreIndex


EMBED_SIDECAR_BASE = os.environ.get("AIFORGE_EMBED_BASE", "http://localhost:8764")


def _make_embed_model() -> Any:
    """Custom LlamaIndex BaseEmbedding that hits our /embed sidecar directly.

    The sidecar only serves AIForge-native /embed (not OpenAI /v1/embeddings),
    so we bypass OpenAIEmbedding and call the native endpoint.
    """
    import json
    import urllib.request
    from llama_index.core.embeddings import BaseEmbedding

    base_url = EMBED_SIDECAR_BASE

    def _embed_one(text: str) -> list[float]:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"{base_url}/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        return body["embedding"]

    class _SidecarEmbedding(BaseEmbedding):
        def _get_text_embedding(self, text: str) -> list[float]:
            return _embed_one(text)

        def _get_query_embedding(self, query: str) -> list[float]:
            return _embed_one(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return _embed_one(query)

    return _SidecarEmbedding()


def build_index(
    dsn: str,
    tier_filter: str | None = None,
) -> VectorStoreIndex:
    """Return a read-only VectorStoreIndex over the existing memories table.

    Parameters
    ----------
    dsn:
        libpq-style connection string, e.g. ``host=127.0.0.1 dbname=aiforge``.
    tier_filter:
        Optional tier label (``t1``..``t4``).  When provided the store is
        constructed with a WHERE clause that restricts to that tier only.
    """
    from llama_index.core import VectorStoreIndex
    from llama_index.core.settings import Settings
    from llama_index.vector_stores.postgres import PGVectorStore
    from psycopg import conninfo as _ci

    embed_model = _make_embed_model()
    Settings.embed_model = embed_model

    parsed = _ci.conninfo_to_dict(dsn)
    host = str(parsed.get("host", "127.0.0.1"))
    port = int(parsed.get("port", 5432))
    dbname = str(parsed.get("dbname", "aiforge"))
    user = str(parsed.get("user", ""))
    password = str(parsed.get("password", ""))

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "database": dbname,
        "user": user,
        "password": password,
        "table_name": "memories",
        "embed_dim": 1024,
        "perform_setup": False,
    }
    if tier_filter is not None:
        kwargs["extra_filter_clauses"] = f"tier = '{tier_filter}'"

    vector_store = PGVectorStore.from_params(**kwargs)
    return VectorStoreIndex.from_vector_store(vector_store=vector_store)
