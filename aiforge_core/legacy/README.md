# `aiforge_core.legacy` — DEPRECATED

This package holds pre-archetype indexing / retrieval helpers that
were superseded by the standalone **AiForgeMemory** package.

**Do not import from here.** New code should use:

| What you want | Where it lives now |
|---|---|
| Embeddings / chunks | `aiforge_memory.ingest.embed` |
| Tree-sitter walk + symbols | `aiforge_memory.ingest.treesitter_walk` |
| NL → entities translator | `aiforge_memory.query.translator` |
| ContextBundle for Doer | `aiforge_memory.api.read.context_bundle_for` |
| Graph / RAG retrieval | `aiforge_memory.query.bundle` |

Files in this directory are kept only because some legacy CLI paths
still import them. They will be removed once those imports migrate
to AiForgeMemory.
