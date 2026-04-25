"""Legacy v4 modules. Slated for removal in Phase 11. Do not import from new code.

Contents (all moved here unchanged from their former top-level locations on
2026-04-25 as part of the v5 layout reorg):

- ``embed``          — LM Studio embed sidecar (replaced by NUC pgvector → :Fact embedding)
- ``mcp_graph``      — graph_rag MCP server (replaced by Cypher graph_lookup)
- ``retrieval``      — LlamaIndex pgvector retriever (replaced by Cypher hybrid)
- ``store_v2``       — in-memory ticket store (replaced by Postgres tickets)
- ``graph``          — LangGraph orchestrator nodes/edges/state (replaced by ADK in Phase 9)
- ``rag``            — graph_rag indexer + retriever package

The only active call sites still depending on these modules are the LangGraph
entrypoint (``runtime/graph_runner.py``) and a couple of fall-back paths in
``runtime/memory.py`` / ``planner/tools.py`` / ``doer/tools.py`` /
``runtime/api.py``. Phase 9 (ADK swap) and Phase 11 (legacy delete) will retire
those references.
"""
from __future__ import annotations

__all__ = [
    "embed",
    "mcp_graph",
    "retrieval",
    "store_v2",
    "graph",
    "rag",
]
