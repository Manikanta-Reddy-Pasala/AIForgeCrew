"""Build a read-only LlamaIndex VectorStoreIndex over the existing memories table."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llama_index.core import VectorStoreIndex


EMBED_SIDECAR_BASE = os.environ.get("AIFORGE_EMBED_BASE", "http://localhost:8764")
EMBED_MODEL_NAME = os.environ.get("AIFORGE_EMBED_MODEL", "bge-m3")


def _make_embed_model() -> Any:
    from llama_index.embeddings.openai import OpenAIEmbedding

    class _BgeM3Embedding(OpenAIEmbedding):
        """OpenAIEmbedding subclass that routes to the local bge-m3 sidecar."""

    return _BgeM3Embedding(
        model=EMBED_MODEL_NAME,
        api_base=EMBED_SIDECAR_BASE + "/v1",
        api_key="not-used",
        embed_batch_size=32,
    )


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
